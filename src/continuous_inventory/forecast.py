"""Pluggable point forecasts and rolling-origin backtests; no silent model switching."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from .common import Actor, Conflict, InventoryError, digest, quantity, uid
from .store import StatePort


ALGORITHMS={
    "naive": {"parameters":{},"description":"Repeat the latest observed period"},
    "moving_average": {"parameters":{"window":3},"description":"Mean of the trailing window"},
    "seasonal_naive": {"parameters":{"season_length":12},"description":"Repeat the latest complete season"},
    "exponential_smoothing": {"parameters":{"alpha":"0.3"},"description":"Single exponential smoothing; no trend or season component"},
}


def validate_config(algorithm,parameters):
    if algorithm not in ALGORITHMS:raise InventoryError("Unsupported forecast algorithm")
    defaults=ALGORITHMS[algorithm]["parameters"]
    if set(parameters)-set(defaults):raise InventoryError("Unknown forecast parameter")
    p={**defaults,**parameters}
    for key in ("window","season_length"):
        if key in p and (isinstance(p[key],bool) or not isinstance(p[key],int) or not 1<=p[key]<=10000):
            raise InventoryError(f"{key} must be a positive integer")
    if "alpha" in p:
        alpha=quantity(p["alpha"],positive=True)
        if alpha>1:raise InventoryError("alpha must be in (0,1]")
        p["alpha"]=str(alpha)
    return p


def predict(history, horizon, algorithm, parameters=None):
    if isinstance(horizon,bool) or not isinstance(horizon,int) or not 1<=horizon<=1000:raise InventoryError("Invalid horizon")
    if len(history)>10000:raise InventoryError("History exceeds local forecast budget")
    values=[quantity(x) for x in history]
    if not values:raise InventoryError("History required; use customer-provided projections for cold start")
    p=validate_config(algorithm,parameters or {})
    if algorithm=="naive":result=[values[-1]]*horizon
    elif algorithm=="moving_average":
        if len(values)<p["window"]:raise InventoryError("Insufficient history for moving-average window")
        result=[sum(values[-p["window"]:])/Decimal(p["window"])]*horizon
    elif algorithm=="seasonal_naive":
        period=p["season_length"]
        if len(values)<period:raise InventoryError("Insufficient history for seasonal model")
        season=values[-period:];result=[season[i%period] for i in range(horizon)]
    else:
        level=values[0];alpha=Decimal(p["alpha"])
        for v in values[1:]:level=alpha*v+(1-alpha)*level
        result=[level]*horizon
    return {"algorithm":algorithm,"parameters":p,"point_forecast":[str(v) for v in result],
            "history_hash":digest([str(v) for v in values]),"uncertainty":"Prediction intervals are not implemented"}


def backtest(history, candidates, *, min_train=3, horizon=1):
    if isinstance(min_train,bool) or not isinstance(min_train,int) or min_train<1:raise InventoryError("min_train must be positive")
    if not isinstance(horizon,int) or isinstance(horizon,bool) or horizon<1:raise InventoryError("horizon must be positive")
    if not candidates or len(candidates)>16 or len(history)>512 or horizon>52:
        raise InventoryError("Backtest exceeds bounded local compute budget")
    values=[quantity(v) for v in history]
    if len(values)<min_train+horizon:raise InventoryError("Insufficient history for held-out rolling windows")
    reports=[]
    for candidate in candidates:
        errors=[];actuals=[];folds=[];fail=None
        for origin in range(min_train,len(values)-horizon+1):
            try:f=predict(values[:origin],horizon,candidate["algorithm"],candidate.get("parameters"))
            except InventoryError as exc:fail=str(exc);break
            actual=values[origin:origin+horizon];forecast=[Decimal(v) for v in f["point_forecast"]]
            errors.extend([a-b for a,b in zip(forecast,actual)]);actuals.extend(actual)
            folds.append({"train_end":origin,"forecast":f["point_forecast"],"actual":[str(x) for x in actual]})
        if fail:reports.append({**candidate,"eligible":False,"reason":fail});continue
        abs_error=sum(abs(x) for x in errors);denom=sum(actuals)
        reports.append({**candidate,"eligible":True,"folds":folds,"mae":str(abs_error/len(errors)),
                        "bias":str(sum(errors)/len(errors)),"wape":str(abs_error/denom) if denom else None})
    eligible=[r for r in reports if r["eligible"]]
    winner=min(eligible,key=lambda r:Decimal(r["mae"]))["algorithm"] if eligible else None
    return {"candidates":reports,"recommended_algorithm":winner,"activated":False,
            "note":"All eligible candidates evaluated on the same origins; selection evidence is not an untouched final holdout."}


class ForecastService:
    def __init__(self,store:StatePort):self.store=store

    def configure(self,actor:Actor,*,algorithm,parameters=None,item=None,starts=None,ends=None):
        actor.require("forecast.configure")
        if item is None:actor.require("forecast.business")
        p=validate_config(algorithm,parameters or {})
        if (starts is None)!=(ends is None):raise InventoryError("Season window needs both dates")
        if starts and date.fromisoformat(starts)>date.fromisoformat(ends):raise InventoryError("Invalid seasonal window")
        with self.store.transaction() as db:
            for old in self.store.list(actor.tenant,"forecast_config",db):
                overlap=max(old["starts"] or "0001-01-01",starts or "0001-01-01")<=min(old["ends"] or "9999-12-31",ends or "9999-12-31")
                if old["item"]==item and bool(old["starts"])==bool(starts) and overlap:raise Conflict("Overlapping selection; replace explicitly instead")
            return self.store.put(actor.tenant,"forecast_config",uid("forecast"),{"algorithm":algorithm,"parameters":p,
                "item":item,"starts":starts,"ends":ends,"author":actor.name},db)

    def run(self,actor:Actor,*,item,day,history,horizon):
        actor.require("forecast.read");date.fromisoformat(day)
        configs=[c for c in self.store.list(actor.tenant,"forecast_config") if c["item"] in {None,item}
                 and (c["starts"] or "0001-01-01")<=day<=(c["ends"] or "9999-12-31")]
        if not configs:raise InventoryError("Select a forecast method before running")
        configs.sort(key=lambda c:(c["item"] is not None,c["starts"] is not None));c=configs[-1]
        result=predict(history,horizon,c["algorithm"],c["parameters"])
        result.update(config_id=c["id"],config_version=c["version"],item=item,as_of=day)
        with self.store.transaction() as db:return self.store.put(actor.tenant,"forecast_run",uid("run"),result,db)

    def replace_selection(self, actor: Actor, id: str, expected_version: int, *, algorithm: str, parameters=None):
        actor.require("forecast.configure")
        params = validate_config(algorithm, parameters or {})
        with self.store.transaction() as db:
            old = self.store.get(actor.tenant, "forecast_config", id, db)
            if old["item"] is None:
                actor.require("forecast.business")
            if old["version"] != expected_version:
                raise Conflict("Forecast selection changed; review the current version")
            old.update(algorithm=algorithm, parameters=params, author=actor.name)
            return self.store.put(actor.tenant, "forecast_config", id, old, db, expected_version)
