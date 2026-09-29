from pathlib import Path
import re, math, html
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable, KeepTogether
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'continuous-inventory-technology-strategy.md'
OUT=ROOT/'output/pdf/continuous-inventory-technology-strategy.pdf'
OUT.parent.mkdir(parents=True, exist_ok=True)
INK=colors.HexColor('#182d3b'); ACC=colors.HexColor('#176776'); LIGHT=colors.HexColor('#edf5f5'); GRID=colors.HexColor('#c6d7dc')
W=A4[0]-88
styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name='BodyX',fontName='Helvetica',fontSize=9.5,leading=13.5,textColor=INK,spaceAfter=7))
styles.add(ParagraphStyle(name='H1X',fontName='Helvetica-Bold',fontSize=23,leading=28,textColor=INK,spaceAfter=15))
styles.add(ParagraphStyle(name='H2X',fontName='Helvetica-Bold',fontSize=14,leading=18,textColor=ACC,spaceBefore=15,spaceAfter=9,keepWithNext=True))
styles.add(ParagraphStyle(name='H3X',fontName='Helvetica-Bold',fontSize=11,leading=15,textColor=INK,spaceBefore=10,spaceAfter=6,keepWithNext=True))
styles.add(ParagraphStyle(name='H4X',parent=styles['H3X'],fontSize=10))
styles.add(ParagraphStyle(name='CellX',parent=styles['BodyX'],fontSize=8.4,leading=11,spaceAfter=0))
styles.add(ParagraphStyle(name='NodeX',fontName='Helvetica',fontSize=8.3,leading=10.5,textColor=INK,alignment=TA_CENTER))
styles.add(ParagraphStyle(name='QuoteX',parent=styles['BodyX'],leftIndent=14,borderColor=ACC,borderWidth=1,borderPadding=8,spaceBefore=5,spaceAfter=12))

def fmt(s):
    s=s.replace('\u2011','-').replace('\u2013','-').replace('\u2014',' - ').replace('→',' / ')
    s=html.escape(s)
    s=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',s)
    s=re.sub(r'`([^`]+)`',r'<font name="Courier">\1</font>',s)
    return s

class Diagram(Flowable):
    def __init__(self,nodes,edges,rows,caption=None):
        super().__init__(); self.nodes=nodes; self.edges=edges; self.rows=rows
        self.width=W; self.height=rows*84+12; self.caption=caption
    def draw(self):
        c=self.canv; bw=(W-40)/3; bh=60; xy={}
        for key,(col,row,text) in self.nodes.items():
            x=col*(bw+20); y=self.height-12-row*84-bh; xy[key]=(x,y)
        # Edges first, under the boxes. Optional routing via supplied waypoints.
        for edge in self.edges:
            a,b,*extra=edge; x,y=xy[a]; xx,yy=xy[b]
            if abs(x-xx)<1:
                if yy<y: p=(x+bw/2,y); q=(xx+bw/2,yy+bh)
                else: p=(x+bw/2,y+bh); q=(xx+bw/2,yy)
            elif abs(y-yy)<1:
                if xx>x: p=(x+bw,y+bh/2); q=(xx,yy+bh/2)
                else: p=(x,y+bh/2); q=(xx+bw,yy+bh/2)
            else:
                p=(x+bw/2,y if yy<y else y+bh); q=(xx+bw/2,yy+bh if yy<y else yy)
            points=[p]+(extra[0] if extra else [])+[q]
            c.setStrokeColor(ACC); c.setLineWidth(.85)
            path=c.beginPath(); path.moveTo(*points[0])
            for pt in points[1:]: path.lineTo(*pt)
            c.drawPath(path)
            prev=points[-2]; ang=math.atan2(q[1]-prev[1],q[0]-prev[0]); d=4
            c.setFillColor(ACC); pth=c.beginPath();pth.moveTo(*q)
            pth.lineTo(q[0]-d*math.cos(ang-.5),q[1]-d*math.sin(ang-.5));pth.lineTo(q[0]-d*math.cos(ang+.5),q[1]-d*math.sin(ang+.5));pth.close();c.drawPath(pth,fill=1,stroke=0)
        for key,(col,row,text) in self.nodes.items():
            x,y=xy[key];c.setFillColor(LIGHT);c.setStrokeColor(GRID);c.roundRect(x,y,bw,bh,5,fill=1,stroke=1)
            p=Paragraph(text,styles['NodeX']);_,h=p.wrap(bw-14,bh-6);p.drawOn(c,x+7,y+(bh-h)/2)

# Print-native vector versions of the seven strategy diagrams.
diagrams=[]
diagrams.append(Diagram({
'A':(0,0,'<b>Customer direction</b><br/>Goals, process, permissions'), 'B':(1,0,'<b>Understand</b><br/>Inventory, demand, supply, cost'), 'C':(2,0,'<b>Prepare</b><br/>Buy, build, transfer, fulfill, allocate cost'),
'D':(2,1,'<b>Authorize</b><br/>Approval or explicit bounded delegation'),'E':(1,1,'<b>Execute</b><br/>Existing operational and financial capabilities'),'F':(0,1,'<b>Verify</b><br/>Service, investment, close readiness'),
'G':(0,2,'<b>Customer steering</b><br/>Teaching and corrections'),'H':(1,2,'<b>Learn</b><br/>Customer-specific outcomes')},[('A','B'),('B','C'),('C','D'),('D','E'),('E','F'),('F','H'),('G','H')],3))
diagrams.append(Diagram({
'A':(0,0,'<b>Item Master</b><br/>Items, BOMs, UOMs<br/>Categories, vendors'),'B':(1,0,'<b>IOM</b><br/>Locations, activities<br/>Manufacturing, warehouse'),'C':(2,0,'<b>1TS</b><br/>Orders, finance, quantity<br/>FIFO / MAC'),
'D':(0,1,'<b>Landed cost</b><br/>Manual bill mapping<br/>Ownership to confirm'),'E':(1,1,'<b>BI and reporting</b>'),'F':(2,1,'<b>Search and audit</b>')},[],2))
diagrams.append(Diagram({
'A':(1,0,'<b>Customer process</b><br/>For this item and season'),'B':(0,1,'<b>Bulk / stock-based</b><br/>Forecast and stock policy'),'C':(1,1,'<b>Order-driven</b><br/>Confirmed sales orders'),'D':(2,1,'<b>Hybrid</b><br/>Stock components or batch eligible orders'),
'E':(1,2,'<b>Shared demand / supply</b><br/>Net orders, forecasts, stock, reservations, inbound'), 'F':(1,3,'<b>Coordinated feasible plan</b><br/>Buy, build, transfer, fulfill'),'G':(1,4,'<b>Authorize</b><br/>Execute and verify')},[('A','B'),('A','C'),('A','D'),('B','E'),('C','E'),('D','E'),('E','F'),('F','G')],5))
diagrams.append(Diagram({
'A':(0,0,'<b>Declared process</b><br/>Including no-history customers'),'B':(1,0,'<b>Observed practice</b><br/>Transactions and outcomes'),'C':(2,0,'<b>Candidate patterns</b><br/>Confidence and exceptions'),
'D':(0,1,'<b>Business understanding</b><br/>Scope, season, evidence, effective dates'),'E':(1,1,'<b>Recommendation</b><br/>Reasons and tradeoffs'),'F':(2,1,'<b>Review / correct</b><br/>Customer feedback'),
'G':(2,2,'<b>Clarify scope</b><br/>Action, item, season, business'),'H':(1,2,'<b>Apply instruction</b><br/>Update affected proposals'),'I':(0,2,'<b>Authorize and execute</b><br/>Verify results; feed learning')},[('A','D'),('B','C'),('D','E'),('E','F'),('F','G'),('G','H'),('H','I')],3))
diagrams.append(Diagram({
'A':(0,0,'<b>Charge bill</b><br/>Freight, shipping, insurance'),'B':(1,0,'<b>Identify eligible charge</b><br/>Setup and learned candidates'),'C':(2,0,'<b>Match inventory bills</b><br/>References and evidence'),
'D':(2,1,'<b>Prepare allocation</b><br/>Method, amounts, remainder'),'E':(1,1,'<b>Evidence + authorization</b><br/>Review ambiguity; approve or apply delegation'),'F':(0,1,'<b>Apply allocation</b><br/>Through owning capabilities'),
'G':(0,2,'<b>Verify</b><br/>Totals, costing, posting'),'H':(1,2,'<b>Update</b><br/>Cost outlook and close package'),'I':(2,2,'<b>Learn from corrections</b><br/>Scoped matching improvements')},[('A','B'),('B','C'),('C','D'),('D','E'),('E','F'),('F','G'),('G','H'),('H','I')],3))
diagrams.append(Diagram({
'A':(0,0,'<b>Operational evidence</b><br/>Activities and transactions'),'B':(1,0,'<b>Cost completion</b><br/>Costing and landed cost'),'C':(2,0,'<b>Reconcile continuously</b><br/>Quantity, valuation, cutoff, posting'),
'D':(2,1,'<b>Resolve exceptions</b><br/>Evidenced corrections'),'E':(1,1,'<b>Close package</b><br/>Versioned inventory review'),'F':(0,1,'<b>Accountant approval</b><br/>Defined scope and entries'),
'G':(0,2,'<b>Execute and verify</b><br/>Revalidate; failed controls return to exceptions'),'H':(1,2,'<b>Inventory close complete</b><br/>Required checks pass'),'I':(2,2,'<b>Broader close authority</b><br/>All accounting workstreams must pass'),
'J':(2,3,'<b>Authorized period close</b><br/>Retain review evidence')},[('A','B'),('B','C'),('C','D'),('D','E'),('E','F'),('F','G'),('G','H'),('H','I'),('I','J')],4))
diagrams.append(Diagram({
'A':(0,0,'<b>Approve each action</b>'),'B':(1,0,'<b>Approve a plan</b>'),'C':(2,0,'<b>Delegate bounded actions</b>'),'D':(2,1,'<b>Delegate an objective</b><br/>Allowed means and limits'),'E':(1,1,'<b>Common controls</b><br/>Validate, execute, verify, audit'),'F':(0,1,'<b>Explicit customer choice</b><br/>Expand, narrow, or revoke authority')},[('A','B'),('B','C'),('C','D'),('D','E'),('F','E')],2))

lines=SRC.read_text().splitlines(); story=[]; i=0; di=0
while i<len(lines):
    line=lines[i].strip()
    if not line: i+=1; continue
    if line.startswith('```mermaid'):
        while i<len(lines)-1:
            i+=1
            if lines[i].strip()=='```':break
        story.append(diagrams[di]); story.append(Spacer(1,10));di+=1;i+=1;continue
    if line.startswith('|'):
        rows=[]
        while i<len(lines) and lines[i].strip().startswith('|'):
            s=lines[i].strip();i+=1
            if re.match(r'^\|[\s:|\-]+\|$',s):continue
            cells=[x.strip() for x in s.strip('|').split('|')]
            rows.append([Paragraph(fmt(x),styles['CellX']) for x in cells])
        n=len(rows[0]); widths=[W*.25,W*.75] if n==2 else [W*.23,W*.385,W*.385]
        t=Table(rows,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),LIGHT),('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,0),.7,ACC),('LINEBELOW',(0,1),(-1,-1),.3,GRID),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
        story.extend([t,Spacer(1,10)]);continue
    if line.startswith('#'):
        level=len(line)-len(line.lstrip('#'));story.append(Paragraph(fmt(line[level:].strip()),styles[{1:'H1X',2:'H2X',3:'H3X',4:'H4X'}[level]]));i+=1;continue
    if line.startswith('> '): story.append(Paragraph(fmt(line[2:]),styles['QuoteX']));i+=1;continue
    if line.startswith('- ') or re.match(r'^\d+\. ',line):
        if line.startswith('- '):body=line[2:];bullet='\u2022'
        else: bullet,body=line.split('. ',1);bullet+='.'
        sty=ParagraphStyle('list',parent=styles['BodyX'],leftIndent=13,firstLineIndent=0,bulletIndent=0)
        story.append(Paragraph(fmt(body),sty,bulletText=bullet));i+=1;continue
    ps=[line];i+=1
    while i<len(lines) and lines[i].strip() and not re.match(r'^(#|\||```|>|- |\d+\. )',lines[i].strip()):ps.append(lines[i].strip());i+=1
    story.append(Paragraph(fmt(' '.join(ps)),styles['BodyX']))

def footer(c,doc):
    c.saveState();c.setStrokeColor(GRID);c.setLineWidth(.4);c.line(44,40,A4[0]-44,40)
    c.setFont('Helvetica',8);c.setFillColor(ACC);c.drawString(44,27,'CONTINUOUS INVENTORY  /  TECHNOLOGY STRATEGY  /  DRAFT')
    c.drawRightString(A4[0]-44,27,str(doc.page));c.restoreState()

doc=SimpleDocTemplate(str(OUT),pagesize=A4,rightMargin=44,leftMargin=44,topMargin=42,bottomMargin=54,title='Continuous Inventory: Technology Strategy',author='Strategy working draft')
doc.build(story,onFirstPage=footer,onLaterPages=footer)
r=PdfReader(str(OUT));print('Created',OUT,'pages',len(r.pages),'diagrams',di)
for n,p in enumerate(r.pages):
    text=p.extract_text() or ''
    if len(text)<160:print('SPARSE PAGE',n+1,len(text))
