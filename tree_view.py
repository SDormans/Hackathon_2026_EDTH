"""A self-contained canvas tree viewer; no libraries or network required."""
import json


HTML = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet search tree</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0d1422;color:#e1e9f4;font:14px system-ui}
header{padding:18px 24px;border-bottom:1px solid #304057}h1{font-size:20px;margin:0 0 8px}
main{display:grid;grid-template-columns:minmax(0,1fr) 330px;height:calc(100vh - 110px)}
canvas{width:100%;height:100%;touch-action:none;cursor:grab}aside{padding:20px;border-left:1px solid #304057;overflow:auto}
button,a{background:#1c304b;color:#e1e9f4;border:1px solid #47617f;padding:7px 10px;border-radius:6px;cursor:pointer}
button{margin:4px 4px 4px 0}a{display:inline-block;text-decoration:none;margin:5px 0}pre{white-space:pre-wrap;line-height:1.6}
p{color:#b0c2d8;line-height:1.5}#stats{color:#b0c2d8}@media(max-width:700px){main{grid-template-columns:minmax(0,1fr) 230px}}
</style>
<header><h1>Fleet search tree</h1><div id="stats"></div>
<button id="best">Best solution</button><button id="root">Root</button><span>Drag to pan · scroll to zoom · click a node</span></header>
<main><canvas id="tree"></canvas><aside><h2>Ranked solutions</h2><div id="solutions"></div>
<h2>Selected node</h2><pre id="details"></pre><p>Each node is an accepted partial fleet plan. Lines show decisions; grey branches skip a contact. Blue nodes serve a contact; gold marks the selected path. Rejected route attempts are counted below.</p><pre id="rejects"></pre></aside></main>
<script>
const data=__DATA__;
const canvas=document.getElementById('tree'),ctx=canvas.getContext('2d');
const levels=new Map(),nodes=new Map();let selected=0,scale=.7,tx=50,ty=50,drag=null,moved=false;
for(const n of data.nodes){let row=levels.get(n.depth)||0;levels.set(n.depth,row+1);nodes.set(n.id,{...n,x:n.depth*170,y:row*24});}
let path=new Set();
function choose(id,center=true){selected=id;path=new Set();let n=nodes.get(id);while(n){path.add(n.id);n=nodes.get(n.parent);}
 const node=nodes.get(id);document.getElementById('details').textContent=JSON.stringify(node?{id:node.id,parent:node.parent,decision:node.decision,score:node.score,served:node.served}:null,null,2);
 if(center&&node){tx=canvas.clientWidth/2-node.x*scale;ty=canvas.clientHeight/2-node.y*scale;}draw();}
function draw(){const ratio=window.devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;
 if(canvas.width!==Math.round(w*ratio)||canvas.height!==Math.round(h*ratio)){canvas.width=Math.round(w*ratio);canvas.height=Math.round(h*ratio);}
 ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,w,h);ctx.translate(tx,ty);ctx.scale(scale,scale);
 const left=-tx/scale,right=(w-tx)/scale,top=-ty/scale,bottom=(h-ty)/scale;
 for(const n of nodes.values()){const p=nodes.get(n.parent);if(!p||Math.max(n.x,p.x)<left||Math.min(n.x,p.x)>right||Math.max(n.y,p.y)<top||Math.min(n.y,p.y)>bottom)continue;
 ctx.strokeStyle=path.has(n.id)?'#ffd36a':'#293a52';ctx.lineWidth=(path.has(n.id)?2:1)/scale;ctx.beginPath();ctx.moveTo(p.x,p.y);ctx.lineTo(n.x,n.y);ctx.stroke();}
 for(const n of nodes.values()){if(n.x<left-20||n.x>right+20||n.y<top-20||n.y>bottom+20)continue;
 ctx.fillStyle=path.has(n.id)?'#ffd36a':n.decision.kind==='skip'?'#546078':'#5eaafa';ctx.beginPath();ctx.arc(n.x,n.y,n.id===selected?7:4,0,Math.PI*2);ctx.fill();
 if(scale>.65){ctx.fillStyle='#e1e9f4';ctx.font='11px system-ui';ctx.fillText(n.score.toFixed(1),n.x+10,n.y+4);}}
}
document.getElementById('stats').textContent=`${data.nodes.length} nodes · ${data.candidate_checks} candidate checks · ${data.elapsed_s.toFixed(2)} s · beam ${data.beam_width}`;
document.getElementById('rejects').textContent='Rejected attempts by rule\n'+JSON.stringify(data.rejections,null,2);
for(const s of data.solutions){const row=document.createElement('div'),button=document.createElement('button'),link=document.createElement('a');
button.textContent=`#${s.rank} · ${s.score.toFixed(2)} points`;button.onclick=()=>choose(s.node);link.textContent='Plan JSON';link.href=`plan-${String(s.rank).padStart(2,'0')}.json`;row.append(button,link);document.getElementById('solutions').append(row);}
canvas.onpointerdown=e=>{drag={x:e.clientX,y:e.clientY,tx,ty};moved=false;canvas.setPointerCapture(e.pointerId)};
canvas.onpointermove=e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.hypot(dx,dy)>4)moved=true;tx=drag.tx+dx;ty=drag.ty+dy;draw()};
canvas.onpointerup=e=>{if(!moved){const box=canvas.getBoundingClientRect(),x=(e.clientX-box.left-tx)/scale,y=(e.clientY-box.top-ty)/scale;let best=null,dist=15/scale;for(const n of nodes.values()){const d=Math.hypot(n.x-x,n.y-y);if(d<dist){best=n;dist=d}}if(best)choose(best.id,false);}drag=null};
canvas.addEventListener('wheel',e=>{e.preventDefault();const box=canvas.getBoundingClientRect(),x=e.clientX-box.left,y=e.clientY-box.top,old=scale;scale=Math.max(.03,Math.min(4,scale*Math.exp(-e.deltaY*.001)));tx=x-(x-tx)*scale/old;ty=y-(y-ty)*scale/old;draw()},{passive:false});
document.getElementById('best').onclick=()=>choose(data.solutions[0].node);document.getElementById('root').onclick=()=>choose(0);
window.onresize=draw;choose(data.solutions[0].node);
</script></html>'''


def save_tree(report, path):
    payload = json.dumps(report, separators=(",", ":")).replace("<", "\\u003c")
    path.write_text(HTML.replace("__DATA__", payload), encoding="utf-8")
