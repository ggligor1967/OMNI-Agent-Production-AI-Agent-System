"""
OMNI AGENT - Web Dashboard
Real-time HTML dashboard served over aiohttp.
Accessible at http://localhost:8000/dashboard
"""
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from agent.core import OmniAgent

logger = logging.getLogger(__name__)

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OMNI Agent — Control Panel</title>
<style>
:root{
  --bg:#0f0f13;--card:#1a1a24;--card2:#12121c;--border:#2a2a3a;
  --green:#00ff88;--blue:#4a9eff;--red:#ff4466;--yellow:#ffcc44;
  --purple:#b069ff;--cyan:#00e5ff;
  --text:#e0e0f0;--muted:#7070a0;--font:'Courier New',monospace;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;}
#hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;border-bottom:1px solid var(--border);background:var(--card2);}
#hdr h1{color:var(--green);font-size:1.1rem;}
#hdr .sub{color:var(--muted);font-size:0.75rem;margin-top:2px;}
#key-wrap{display:flex;gap:8px;align-items:center;}
#key-inp{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:4px;font-family:var(--font);font-size:0.8rem;width:320px;}
#tabs{display:flex;gap:0;border-bottom:1px solid var(--border);background:var(--card2);padding:0 20px;overflow-x:auto;}
.tab{padding:10px 16px;cursor:pointer;font-size:0.8rem;color:var(--muted);border-bottom:2px solid transparent;white-space:nowrap;transition:.15s;}
.tab:hover{color:var(--text);}
.tab.active{color:var(--green);border-bottom-color:var(--green);}
#panels{padding:20px;}
.panel{display:none;}
.panel.active{display:block;}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
.gX{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;}
.ct{color:var(--blue);font-size:0.72rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;}
.stat{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:0.83rem;}
.stat:last-child{border-bottom:none;}
.sl{color:var(--muted);}
.sv{font-weight:bold;}
.ok{color:var(--green);}
.fail{color:var(--red);}
.warn{color:var(--yellow);}
.info{color:var(--blue);}
.row{display:flex;gap:8px;margin-bottom:10px;align-items:stretch;}
.row.col{flex-direction:column;}
label{font-size:0.78rem;color:var(--muted);margin-bottom:4px;display:block;}
input,textarea,select{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:4px;font-family:var(--font);font-size:0.83rem;width:100%;}
textarea{resize:vertical;min-height:80px;}
.ta-lg{min-height:140px;}
select option{background:var(--card);}
.btn{display:inline-block;padding:6px 14px;border-radius:4px;cursor:pointer;font-family:var(--font);font-size:0.82rem;border:none;transition:.15s;}
.btn-g{background:#00ff8822;color:var(--green);border:1px solid #00ff8844;}
.btn-g:hover{background:#00ff8844;}
.btn-b{background:#4a9eff22;color:var(--blue);border:1px solid #4a9eff44;}
.btn-b:hover{background:#4a9eff44;}
.btn-r{background:#ff446622;color:var(--red);border:1px solid #ff446644;}
.btn-r:hover{background:#ff446644;}
.btn-y{background:#ffcc4422;color:var(--yellow);border:1px solid #ffcc4444;}
.btn-y:hover{background:#ffcc4444;}
.out{background:var(--card2);border:1px solid var(--border);border-radius:4px;padding:12px;font-size:0.82rem;white-space:pre-wrap;word-break:break-all;max-height:380px;overflow-y:auto;color:var(--text);}
.out-sm{max-height:200px;}
table{width:100%;border-collapse:collapse;font-size:0.8rem;}
th{text-align:left;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border);}
td{padding:5px 8px;border-bottom:1px solid #1e1e2a;}
.bdg{display:inline-block;padding:1px 7px;border-radius:10px;font-size:0.72rem;}
.bdg-g{background:#00ff8820;color:var(--green);}
.bdg-b{background:#4a9eff20;color:var(--blue);}
.bdg-r{background:#ff446620;color:var(--red);}
.bdg-y{background:#ffcc4420;color:var(--yellow);}
.bdg-p{background:#b069ff20;color:var(--purple);}
.log-line{padding:2px 0;border-bottom:1px solid #16161f;font-size:0.78rem;}
#chat-msgs{height:340px;overflow-y:auto;padding:10px;background:var(--card2);border:1px solid var(--border);border-radius:4px;margin-bottom:10px;}
.msg{margin-bottom:10px;}
.msg-role{font-size:0.72rem;color:var(--muted);margin-bottom:2px;}
.msg-body{font-size:0.85rem;line-height:1.5;}
@keyframes spin{to{transform:rotate(360deg);}}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--green);border-radius:50%;animation:spin .6s linear infinite;}
.cmp-col{flex:1;min-width:0;}
.cmp-title{font-size:0.75rem;color:var(--muted);margin-bottom:4px;}
a{color:var(--blue);text-decoration:none;}
a:hover{text-decoration:underline;}
</style>
</head>
<body>

<div id="hdr">
  <div>
    <div><h1>&#x2B21; OMNI Agent &mdash; Control Panel</h1></div>
    <div class="sub">Production AI Agent System &middot; <span id="srv-status">&hellip;</span></div>
  </div>
  <div id="key-wrap">
    <input id="key-inp" type="password" placeholder="API Key (X-API-Key)" />
    <button class="btn btn-g" onclick="saveKey()">Save</button>
    <span id="key-ok" style="font-size:0.75rem;color:var(--muted)"></span>
  </div>
</div>

<div id="tabs">
  <div class="tab active" onclick="showTab('overview')">&#x1F4CA; Overview</div>
  <div class="tab" onclick="showTab('chat')">&#x1F4AC; Chat</div>
  <div class="tab" onclick="showTab('compare')">&#x2696;&#xFE0F; Compare</div>
  <div class="tab" onclick="showTab('rag')">&#x1F4DA; RAG</div>
  <div class="tab" onclick="showTab('pipelines')">&#x1F504; Pipelines</div>
  <div class="tab" onclick="showTab('sandbox')">&#x1F5A5;&#xFE0F; Sandbox</div>
  <div class="tab" onclick="showTab('kg')">&#x1F578;&#xFE0F; KG</div>
  <div class="tab" onclick="showTab('tools')">&#x1F527; Tools</div>
  <div class="tab" onclick="showTab('models')">&#x1F916; Models</div>
</div>

<div id="panels">

<!-- OVERVIEW -->
<div class="panel active" id="p-overview">
  <div class="gX" id="ov-stats"></div>
  <div style="margin-top:16px;" class="card">
    <div class="ct">ADR Tanker Job Search — Quick Launch</div>
    <div class="row">
      <div style="flex:1">
        <label>Export Format</label>
        <select id="ov-job-search-export">
          <option value="html" selected>HTML + JSON</option>
          <option value="json">JSON only</option>
          <option value="csv">CSV + JSON</option>
          <option value="all">HTML + CSV + JSON</option>
        </select>
      </div>
      <div style="flex:1">
        <label>Verbose Logging</label>
        <select id="ov-job-search-verbose">
          <option value="false" selected>No</option>
          <option value="true">Yes</option>
        </select>
      </div>
    </div>
    <div class="row col">
      <label>Custom Output Directory (optional)</label>
      <input id="ov-job-search-output-dir" placeholder="data/job_results/manual_runs" />
    </div>
    <button class="btn btn-g" onclick="runAdrJobSearch('ov-job-search')">&#x25BA; Quick Run ADR Job Search</button>
    <div id="ov-job-search-status" style="font-size:0.75rem;color:var(--muted);margin-top:8px;">Launches the dedicated ADR tanker search directly from Overview.</div>
    <div id="ov-job-search-links" style="font-size:0.78rem;margin-top:8px;"></div>
    <div id="ov-job-search-out" class="out out-sm" style="margin-top:8px;">Ready. This quick-launch card uses the same dedicated OMNI tool as the Pipelines shortcut.</div>
  </div>
  <div style="margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:16px;">
    <div class="card">
      <div class="ct">Scheduled Jobs</div>
      <div id="ov-jobs"><span class="spin"></span></div>
    </div>
    <div class="card">
      <div class="ct">Cache Stats</div>
      <div id="ov-cache"><span class="spin"></span></div>
    </div>
  </div>
  <div style="margin-top:16px;" class="card">
    <div class="ct">Audit Log
      <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadAudit()">Refresh</button>
    </div>
    <div id="ov-audit" class="out out-sm">Loading&hellip;</div>
  </div>
</div>

<!-- CHAT -->
<div class="panel" id="p-chat">
  <div class="grid2">
    <div>
      <div class="row">
        <div style="flex:1">
          <label>Model (blank = auto-route)</label>
          <select id="chat-model"><option value="">Auto-route</option></select>
        </div>
        <div style="flex:1">
          <label>Session ID</label>
          <input id="chat-session" value="demo" />
        </div>
      </div>
      <div id="chat-msgs"></div>
      <div class="row">
        <input id="chat-inp" placeholder="Message&hellip;" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat();}" style="flex:1" />
        <button class="btn btn-g" onclick="sendChat()">Send</button>
        <button class="btn btn-r" onclick="clearChat()">Clear</button>
      </div>
      <div id="chat-routing" style="font-size:0.75rem;color:var(--muted);margin-top:6px;"></div>
    </div>
    <div>
      <div class="card">
        <div class="ct">Last Response Metadata</div>
        <div id="chat-meta" class="out out-sm">&mdash;</div>
      </div>
      <div class="card" style="margin-top:12px;">
        <div class="ct">History</div>
        <div class="row" style="margin-top:8px;">
          <button class="btn btn-b" onclick="loadHistory()">Load History</button>
          <button class="btn btn-y" onclick="clearHistory()">Clear History</button>
        </div>
        <div id="chat-hist" class="out out-sm" style="margin-top:8px;">&mdash;</div>
      </div>
    </div>
  </div>
</div>

<!-- COMPARE -->
<div class="panel" id="p-compare">
  <div class="card" style="margin-bottom:14px;">
    <div class="ct">Compare Models in Parallel</div>
    <div class="row">
      <div style="flex:2">
        <label>Prompt</label>
        <textarea id="cmp-prompt" class="ta-lg" placeholder="Enter prompt to compare across models&hellip;"></textarea>
      </div>
      <div style="flex:1">
        <label>Models (one per line)</label>
        <textarea id="cmp-models" style="min-height:80px;" placeholder="llama3.2:cloud&#10;mistral:cloud&#10;gemma:cloud"></textarea>
        <div style="margin-top:8px;">
          <button class="btn btn-g" onclick="runCompare()">&#x25BA; Run Compare</button>
        </div>
        <div id="cmp-status" style="font-size:0.75rem;color:var(--muted);margin-top:6px;"></div>
      </div>
    </div>
  </div>
  <div id="cmp-results" style="display:flex;gap:12px;flex-wrap:wrap;"></div>
</div>

<!-- RAG -->
<div class="panel" id="p-rag">
  <div class="grid2" style="margin-bottom:14px;">
    <div class="card">
      <div class="ct">Ingest Document</div>
      <div class="row col">
        <label>Text Content</label>
        <textarea id="rag-text" class="ta-lg" placeholder="Paste text to ingest&hellip;"></textarea>
      </div>
      <div class="row">
        <div style="flex:1"><label>Doc ID</label><input id="rag-docid" placeholder="doc-001" /></div>
        <div style="flex:1"><label>Source</label><input id="rag-src" placeholder="manual" /></div>
      </div>
      <button class="btn btn-g" onclick="ragIngest()">Ingest</button>
      <div id="rag-ingest-out" class="out out-sm" style="margin-top:8px;"></div>
    </div>
    <div class="card">
      <div class="ct">Semantic Query</div>
      <div class="row col">
        <label>Query</label>
        <input id="rag-query" placeholder="What is&hellip;?" />
      </div>
      <div class="row">
        <div style="flex:1"><label>Top-K</label><input id="rag-topk" value="5" type="number" min="1" max="20" /></div>
        <div style="flex:1"><label>Doc ID filter (opt)</label><input id="rag-filter" placeholder="" /></div>
      </div>
      <button class="btn btn-b" onclick="ragQuery()">Query</button>
      <div id="rag-query-out" class="out" style="margin-top:8px;"></div>
    </div>
  </div>
  <div class="card">
    <div class="ct">Document Store
      <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadRagDocs()">Refresh</button>
    </div>
    <div id="rag-docs"><span class="spin"></span></div>
  </div>
</div>

<!-- PIPELINES -->
<div class="panel" id="p-pipelines">
  <div class="grid2">
    <div>
      <div class="card" style="margin-bottom:12px;">
        <div class="ct">Pipelines
          <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadPipelines()">Refresh</button>
        </div>
        <div id="pipe-list"><span class="spin"></span></div>
      </div>
      <div class="card">
        <div class="ct">Run Pipeline</div>
        <div class="row"><div style="flex:1"><label>Pipeline ID</label><input id="pipe-id" placeholder="research" /></div></div>
        <div class="row col"><label>Initial Context (JSON)</label><textarea id="pipe-ctx">{"query": "hello world"}</textarea></div>
        <button class="btn btn-g" onclick="runPipeline()">&#x25BA; Execute</button>
        <div id="pipe-out" class="out" style="margin-top:8px;"></div>
      </div>
    </div>
    <div>
      <div class="card" style="margin-bottom:12px;">
        <div class="ct">Workflows
          <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadWorkflows()">Refresh</button>
        </div>
        <div id="wf-list"><span class="spin"></span></div>
      </div>
      <div class="card" style="margin-bottom:12px;">
        <div class="ct">ADR Tanker Job Search</div>
        <div class="row">
          <div style="flex:1">
            <label>Export Format</label>
            <select id="job-search-export">
              <option value="html" selected>HTML + JSON</option>
              <option value="json">JSON only</option>
              <option value="csv">CSV + JSON</option>
              <option value="all">HTML + CSV + JSON</option>
            </select>
          </div>
          <div style="flex:1">
            <label>Verbose Logging</label>
            <select id="job-search-verbose">
              <option value="false" selected>No</option>
              <option value="true">Yes</option>
            </select>
          </div>
        </div>
        <div class="row col">
          <label>Custom Output Directory (optional)</label>
          <input id="job-search-output-dir" placeholder="data/job_results/manual_runs" />
        </div>
        <button class="btn btn-g" onclick="runAdrJobSearch('job-search')">&#x25BA; Run ADR Job Search</button>
        <div id="job-search-status" style="font-size:0.75rem;color:var(--muted);margin-top:8px;">One click launches `run_job_search_tank_adr_improved`.</div>
        <div id="job-search-links" style="font-size:0.78rem;margin-top:8px;"></div>
        <div id="job-search-out" class="out out-sm" style="margin-top:8px;">Ready. This shortcut uses the dedicated OMNI tool and returns the latest report paths automatically.</div>
      </div>
      <div class="card">
        <div class="ct">Structured Output Parser</div>
        <div class="row col"><label>Prompt</label><textarea id="so-prompt" placeholder="Extract name, age and city from: John, 30, New York"></textarea></div>
        <div class="row col"><label>JSON Schema</label><textarea id="so-schema">{"type":"object","properties":{"name":{"type":"string"},"age":{"type":"integer"},"city":{"type":"string"}}}</textarea></div>
        <button class="btn btn-b" onclick="runStructured()">&#x25BA; Parse</button>
        <div id="so-out" class="out out-sm" style="margin-top:8px;"></div>
      </div>
    </div>
  </div>
</div>

<!-- SANDBOX -->
<div class="panel" id="p-sandbox">
  <div class="grid2">
    <div class="card">
      <div class="ct">Code Execution</div>
      <div class="row">
        <div style="flex:1">
          <label>Language</label>
          <select id="sb-lang">
            <option value="python">Python</option>
            <option value="javascript">JavaScript</option>
            <option value="bash">Bash</option>
          </select>
        </div>
        <div style="flex:1"><label>Timeout (s)</label><input id="sb-timeout" value="10" type="number" /></div>
      </div>
      <div class="row col">
        <label>Code</label>
        <textarea id="sb-code" class="ta-lg" placeholder="print('hello world')"></textarea>
      </div>
      <button class="btn btn-g" onclick="runSandbox()">&#x25BA; Execute</button>
      <div id="sb-out" class="out" style="margin-top:8px;"></div>
    </div>
    <div class="card">
      <div class="ct">Execution History
        <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadSandboxHistory()">Refresh</button>
      </div>
      <div id="sb-hist"><span class="spin"></span></div>
    </div>
  </div>
</div>

<!-- KG -->
<div class="panel" id="p-kg">
  <div class="grid2" style="margin-bottom:14px;">
    <div class="card">
      <div class="ct">Extract Entities &amp; Relations</div>
      <div class="row col"><label>Text</label><textarea id="kg-text" class="ta-lg" placeholder="Alice works at Acme Corp. Bob is Alice's manager."></textarea></div>
      <button class="btn btn-g" onclick="kgExtract()">Extract</button>
      <div id="kg-extract-out" class="out out-sm" style="margin-top:8px;"></div>
    </div>
    <div class="card">
      <div class="ct">Search &amp; Path</div>
      <div class="row col"><label>Search Query</label><input id="kg-search" placeholder="Alice" /></div>
      <div class="row">
        <div style="flex:1"><label>From Node</label><input id="kg-from" placeholder="Alice" /></div>
        <div style="flex:1"><label>To Node</label><input id="kg-to" placeholder="Acme Corp" /></div>
      </div>
      <div class="row" style="gap:8px;">
        <button class="btn btn-b" onclick="kgSearch()">Search</button>
        <button class="btn btn-y" onclick="kgPath()">Find Path</button>
        <button class="btn btn-b" onclick="kgStats()">Stats</button>
      </div>
      <div id="kg-search-out" class="out out-sm" style="margin-top:8px;"></div>
    </div>
  </div>
  <div class="card">
    <div class="ct">Export
      <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="kgExport()">Export JSON</button>
    </div>
    <div id="kg-export-out" class="out out-sm">Click "Export JSON" to dump the full graph.</div>
  </div>
</div>

<!-- TOOLS -->
<div class="panel" id="p-tools">
  <div class="grid2" style="margin-bottom:14px;">
    <div class="card">
      <div class="ct">Tool Registry
        <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadTools()">Refresh</button>
      </div>
      <div id="tools-list"><span class="spin"></span></div>
      <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;">
        <button class="btn btn-y" onclick="loadToolSchema('openai')">OpenAI Schema</button>
        <button class="btn btn-y" onclick="loadToolSchema('anthropic')">Anthropic Schema</button>
      </div>
      <div id="tools-schema-out" class="out out-sm" style="margin-top:8px;"></div>
    </div>
    <div class="card">
      <div class="ct">Call Tool</div>
      <div class="row col"><label>Tool Name</label><input id="tool-name" placeholder="search" /></div>
      <div class="row col"><label>Parameters (JSON)</label><textarea id="tool-params">{"query": "test"}</textarea></div>
      <button class="btn btn-g" onclick="callTool()">&#x25BA; Call</button>
      <div id="tool-call-out" class="out out-sm" style="margin-top:8px;"></div>
    </div>
  </div>
  <div class="grid2">
    <div class="card">
      <div class="ct">Vision Analysis</div>
      <div class="row col"><label>Image URL or base64</label><input id="vis-url" placeholder="https://&hellip;/image.jpg" /></div>
      <div class="row col"><label>Prompt</label><input id="vis-prompt" value="Describe this image" /></div>
      <button class="btn btn-b" onclick="runVision()">Analyze</button>
      <div id="vis-out" class="out out-sm" style="margin-top:8px;"></div>
    </div>
    <div class="card">
      <div class="ct">Distributed Tracing
        <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadTracing()">Refresh</button>
      </div>
      <div id="trace-out"><span class="spin"></span></div>
    </div>
  </div>
</div>

<!-- MODELS -->
<div class="panel" id="p-models">
  <div class="card" style="margin-bottom:14px;overflow-x:auto;">
    <div class="ct">Model Catalog
      <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="fetchModels()">Refresh</button>
    </div>
    <table>
      <thead><tr><th>ID</th><th>Provider</th><th>Tier</th><th>Context</th><th>Best For</th><th>Capabilities</th></tr></thead>
      <tbody id="models-tbody"><tr><td colspan="6"><span class="spin"></span></td></tr></tbody>
    </table>
  </div>
  <div class="grid2">
    <div class="card">
      <div class="ct">Personas
        <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadPersonas()">Refresh</button>
      </div>
      <div id="persona-list"><span class="spin"></span></div>
      <div style="margin-top:10px;">
        <label>Activate Persona</label>
        <div class="row" style="margin-top:4px;">
          <input id="persona-set" placeholder="persona id" style="flex:1" />
          <button class="btn btn-g" onclick="setPersona()">Set</button>
        </div>
        <div id="persona-out" class="out out-sm" style="margin-top:6px;"></div>
      </div>
    </div>
    <div>
      <div class="card" style="margin-bottom:12px;">
        <div class="ct">Prompt Templates
          <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadTemplates()">Refresh</button>
        </div>
        <div id="tmpl-list"><span class="spin"></span></div>
      </div>
      <div class="card">
        <div class="ct">Stored Memories
          <button class="btn btn-b" style="float:right;padding:2px 8px;font-size:0.72rem" onclick="loadMemories()">Refresh</button>
        </div>
        <div id="mem-list"><span class="spin"></span></div>
      </div>
    </div>
  </div>
</div>

</div><!-- /panels -->

<script>
// ── Auth ──────────────────────────────────────────────────────────
function saveKey(){
  const v=document.getElementById('key-inp').value.trim();
  if(v){localStorage.setItem('omni_api_key',v);document.getElementById('key-ok').textContent='✓ saved';}
}
function getKey(){return localStorage.getItem('omni_api_key')||'';}
(function(){const k=getKey();if(k)document.getElementById('key-inp').value=k;})();
async function apiFetch(url,opts={}){
  const h=Object.assign({'Content-Type':'application/json'},opts.headers||{});
  const k=getKey();if(k)h['X-API-Key']=k;
  return fetch(url,Object.assign({},opts,{headers:h}));
}

// ── Tabs ──────────────────────────────────────────────────────────
const TAB_NAMES=['overview','chat','compare','rag','pipelines','sandbox','kg','tools','models'];
function showTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  const idx=TAB_NAMES.indexOf(name);
  if(idx>=0)document.querySelectorAll('.tab')[idx].classList.add('active');
  document.getElementById('p-'+name).classList.add('active');
  const lazy={
    chat:loadChatModels,
    compare:loadCompareModels,
    rag:loadRagDocs,
    pipelines:()=>{loadPipelines();loadWorkflows();},
    sandbox:loadSandboxHistory,
    tools:loadTools,
    kg:kgStats,
    models:()=>{fetchModels();loadPersonas();loadTemplates();loadMemories();}
  };
  if(lazy[name])lazy[name]();
}

// ── Overview ──────────────────────────────────────────────────────
async function initOverview(){
  try{
    const r=await apiFetch('/status');const d=await r.json();
    document.getElementById('srv-status').innerHTML='<span class="ok">●</span> '+(d.status||'running');
    const cards=[
      {title:'Status',stats:[{l:'State',v:d.status,c:'ok'},{l:'Version',v:d.version||'—'},{l:'Mode',v:d.mode||'api'}]},
      {title:'Agent',stats:[{l:'Models',v:d.models||'—'},{l:'Skills',v:d.skills||'—'},{l:'Pipelines',v:d.pipelines||'—'}]},
      {title:'Memory',stats:[{l:'Conversations',v:d.conversations||'—'},{l:'Memories',v:d.memories||'—'},{l:'DB',v:d.db||'SQLite',c:'info'}]},
    ];
    document.getElementById('ov-stats').innerHTML=cards.map(c=>
      '<div class="card"><div class="ct">'+c.title+'</div>'+
      c.stats.map(s=>'<div class="stat"><span class="sl">'+s.l+'</span><span class="sv '+(s.c?s.c+'"':'"')+'>'+s.v+'</span></div>').join('')+
      '</div>'
    ).join('');
  }catch(e){document.getElementById('srv-status').innerHTML='<span class="fail">✗ offline</span>';}
  try{
    const rj=await apiFetch('/status');const dj=await rj.json();
    const jobs=dj.jobs||[];
    document.getElementById('ov-jobs').innerHTML=jobs.length?
      jobs.map(j=>'<div class="stat"><span class="sl">'+(j.id||j.name)+'</span><span class="sv info">'+(j.schedule||j.next_run||j.status||'—')+'</span></div>').join(''):
      '<span style="color:var(--muted);font-size:0.8rem">No scheduled jobs</span>';
  }catch(e){document.getElementById('ov-jobs').textContent='unavailable';}
  try{
    const r=await apiFetch('/cache/stats');const d=await r.json();
    const e=d.stats||d;
    document.getElementById('ov-cache').innerHTML=Object.entries(e).map(([k,v])=>
      '<div class="stat"><span class="sl">'+k+'</span><span class="sv">'+JSON.stringify(v)+'</span></div>'
    ).join('')||'<span style="color:var(--muted)">No stats</span>';
  }catch(e){document.getElementById('ov-cache').textContent='unavailable';}
  loadAudit();
}
async function loadAudit(){
  try{
    const r=await apiFetch('/audit?limit=20');const d=await r.json();
    const lines=(d.log||[]).slice().reverse().map(l=>
      '<div class="log-line"><span style="color:var(--muted)">'+(l.timestamp||'')+'</span> <span style="color:var(--blue)">'+(l.action||l.event||'')+'</span> '+(l.details||l.message||'')+'</div>'
    );
    document.getElementById('ov-audit').innerHTML=lines.join('')||'Empty audit log';
  }catch(e){document.getElementById('ov-audit').textContent='unavailable';}
}

// ── Chat ──────────────────────────────────────────────────────────
async function loadChatModels(){
  try{
    const r=await apiFetch('/models');const d=await r.json();
    const sel=document.getElementById('chat-model');
    const current=sel.value;
    const available=(d.models||[]).filter(m=>m.available!==false);
    sel.innerHTML='<option value="">Auto-route</option>'+
      available.map(m=>'<option value="'+m.id+'">'+m.id+' ('+m.tier+')</option>').join('');
    sel.value=available.some(m=>m.id===current)?current:'';
  }catch(e){
    const sel=document.getElementById('chat-model');
    sel.innerHTML='<option value="">Auto-route</option>';
  }
}
async function sendChat(){
  const txt=document.getElementById('chat-inp').value.trim();if(!txt)return;
  document.getElementById('chat-inp').value='';
  appendMsg('user',txt);
  const model=document.getElementById('chat-model').value;
  const session=document.getElementById('chat-session').value||'demo';
  try{
    const body={message:txt,session_id:session,model:model||''};
    const r=await apiFetch('/chat',{method:'POST',body:JSON.stringify(body)});
    const d=await r.json();
    if(d.error){appendMsg('error',d.error);}
    else{
      appendMsg('assistant',d.response||d.message||JSON.stringify(d));
      document.getElementById('chat-meta').textContent=JSON.stringify({model:d.model,routed_to:d.routed_to,tokens:d.tokens,latency_ms:d.latency_ms},null,2);
      if(d.routed_to)document.getElementById('chat-routing').textContent='Routed → '+d.routed_to;
    }
  }catch(e){appendMsg('error',String(e));}
}
function appendMsg(role,text){
  const div=document.createElement('div');div.className='msg';
  const colors={user:'var(--blue)',assistant:'var(--green)',error:'var(--red)'};
  div.innerHTML='<div class="msg-role" style="color:'+(colors[role]||'var(--muted)')+'">'+role+'</div><div class="msg-body">'+escHtml(text)+'</div>';
  const box=document.getElementById('chat-msgs');
  box.appendChild(div);box.scrollTop=box.scrollHeight;
}
function clearChat(){document.getElementById('chat-msgs').innerHTML='';document.getElementById('chat-meta').textContent='—';}
async function loadHistory(){
  const sid=document.getElementById('chat-session').value||'demo';
  try{
    const r=await apiFetch('/memories?q=session:'+encodeURIComponent(sid));const d=await r.json();
    document.getElementById('chat-hist').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('chat-hist').textContent=String(e);}
}
async function clearHistory(){
  document.getElementById('chat-hist').textContent='(history clear not supported — use /memories API directly)';
}

// ── Compare ───────────────────────────────────────────────────────
async function loadCompareModels(){
  try{
    const r=await apiFetch('/models');const d=await r.json();
    const avail=(d.models||[]).filter(m=>m.available!==false).map(m=>m.id);
    const ta=document.getElementById('cmp-models');
    if(ta&&!ta.value.trim())ta.value=avail.join('\n');
    document.getElementById('cmp-status').textContent=avail.length+' models available';
  }catch(e){}
}
async function runCompare(){
  const prompt=document.getElementById('cmp-prompt').value.trim();if(!prompt)return;
  const modelsRaw=document.getElementById('cmp-models').value.trim();
  const models=modelsRaw?modelsRaw.split('\n').map(s=>s.trim()).filter(Boolean):[];
  document.getElementById('cmp-results').innerHTML='<span class="spin"></span>';
  document.getElementById('cmp-status').textContent='Running…';
  try{
    const body={prompt};if(models.length)body.models=models;
    const r=await apiFetch('/compare',{method:'POST',body:JSON.stringify(body)});
    const d=await r.json();
    const results=d.results||[];
    document.getElementById('cmp-status').textContent='Done — '+results.length+' responses';
    document.getElementById('cmp-results').innerHTML=results.map(res=>
      '<div class="card cmp-col" style="min-width:260px;flex:1;">'+
      '<div class="cmp-title">'+(res.model||'?')+'</div>'+
      '<div style="font-size:0.72rem;color:var(--muted);margin-bottom:6px;">'+(res.latency_ms||'')+'ms · '+(res.tokens||'')+' tok</div>'+
      '<div class="out out-sm">'+escHtml(res.response||res.error||JSON.stringify(res))+'</div>'+
      '</div>'
    ).join('');
  }catch(e){
    document.getElementById('cmp-status').textContent='Error: '+e;
    document.getElementById('cmp-results').innerHTML='';
  }
}

// ── RAG ───────────────────────────────────────────────────────────
async function ragIngest(){
  const text=document.getElementById('rag-text').value.trim();if(!text)return;
  const doc_id=document.getElementById('rag-docid').value.trim()||'doc-'+Date.now();
  const source=document.getElementById('rag-src').value.trim()||'manual';
  try{
    const r=await apiFetch('/rag/ingest',{method:'POST',body:JSON.stringify({text,doc_id,source})});
    const d=await r.json();
    document.getElementById('rag-ingest-out').textContent=JSON.stringify(d,null,2);
    loadRagDocs();
  }catch(e){document.getElementById('rag-ingest-out').textContent=String(e);}
}
async function ragQuery(){
  const query=document.getElementById('rag-query').value.trim();if(!query)return;
  const top_k=parseInt(document.getElementById('rag-topk').value)||5;
  const filter=document.getElementById('rag-filter').value.trim();
  try{
    const body={query,top_k};if(filter)body.doc_id=filter;
    const r=await apiFetch('/rag/query',{method:'POST',body:JSON.stringify(body)});
    const d=await r.json();
    const chunks=d.results||d.chunks||[];
    document.getElementById('rag-query-out').innerHTML=chunks.map((c,i)=>
      '<div style="border-bottom:1px solid var(--border);padding:6px 0;">'+
      '<div style="font-size:0.72rem;color:var(--muted)">['+i+'] score: '+((c.score||0).toFixed(3))+' · doc: '+(c.doc_id||'—')+'</div>'+
      '<div style="font-size:0.83rem;margin-top:2px;">'+escHtml(c.text||c.content||'')+'</div>'+
      '</div>'
    ).join('')||'No results';
  }catch(e){document.getElementById('rag-query-out').textContent=String(e);}
}
async function loadRagDocs(){
  try{
    const r=await apiFetch('/rag/docs');const d=await r.json();
    const docs=d.documents||[];
    document.getElementById('rag-docs').innerHTML=docs.length?
      '<table><thead><tr><th>Doc ID</th><th>Source</th><th>Chunks</th><th>Created</th></tr></thead><tbody>'+
      docs.map(doc=>'<tr><td>'+(doc.doc_id||'')+'</td><td>'+(doc.source||'')+'</td><td>'+(doc.chunk_count||'—')+'</td><td>'+(doc.created_at||'—')+'</td></tr>').join('')+
      '</tbody></table>':
      '<span style="color:var(--muted);font-size:0.8rem">No documents ingested yet</span>';
  }catch(e){document.getElementById('rag-docs').textContent='unavailable';}
}

// ── Pipelines ─────────────────────────────────────────────────────
async function loadPipelines(){
  try{
    const r=await apiFetch('/pipelines');const d=await r.json();
    const pipes=d.pipelines||[];
    document.getElementById('pipe-list').innerHTML=pipes.length?
      pipes.map(p=>'<div class="stat"><span class="sl">'+(p.id||p.name)+'</span><span class="sv info">'+(p.steps||'?')+' steps</span></div>').join(''):
      '<span style="color:var(--muted);font-size:0.8rem">No pipelines</span>';
  }catch(e){document.getElementById('pipe-list').textContent='unavailable';}
}
async function loadWorkflows(){
  try{
    const r=await apiFetch('/workflows');const d=await r.json();
    const wfs=d.workflows||[];
    document.getElementById('wf-list').innerHTML=wfs.length?
      wfs.map(w=>'<div class="stat"><span class="sl">'+(w.name||w.id)+'</span><span class="sv info">'+(w.steps||'—')+' steps</span></div>').join(''):
      '<span style="color:var(--muted);font-size:0.8rem">No workflows</span>';
  }catch(e){document.getElementById('wf-list').textContent='unavailable';}
}
async function runPipeline(){
  const id=document.getElementById('pipe-id').value.trim();if(!id)return;
  let ctx={};try{ctx=JSON.parse(document.getElementById('pipe-ctx').value||'{}');}catch(e){alert('Invalid JSON context');return;}
  document.getElementById('pipe-out').textContent='Running…';
  try{
    const r=await apiFetch('/pipelines/'+id+'/run',{method:'POST',body:JSON.stringify({context:ctx})});
    const d=await r.json();
    document.getElementById('pipe-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('pipe-out').textContent=String(e);}
}
function adrJobSearchEl(prefix,suffix){
  return document.getElementById(prefix+'-'+suffix);
}
function adrJobSearchArgs(prefix='job-search'){
  const exportFormat=adrJobSearchEl(prefix,'export').value||'html';
  const verbose=adrJobSearchEl(prefix,'verbose').value==='true';
  const outputDir=adrJobSearchEl(prefix,'output-dir').value.trim();
  const args={export_format:exportFormat,verbose};
  if(outputDir)args.output_dir=outputDir;
  return args;
}
function renderAdrJobSearchSummary(prefix,summary){
  const files=summary.report_files||{};
  const htmlUri=summary.html_report_uri||'';
  const statusBits=[
    '<span class="ok">✓ Completed</span>',
    'results: '+(summary.total_results??'—'),
    'high: '+(summary.high_relevance??'—'),
    'sources: '+(summary.unique_sources??'—')
  ];
  adrJobSearchEl(prefix,'status').innerHTML=statusBits.join(' &middot; ');

  const links=[];
  if(htmlUri){
    links.push('<a href="'+escAttr(htmlUri)+'" target="_blank" rel="noopener noreferrer">Open HTML report</a>');
  }
  Object.entries(files).forEach(([name,path])=>{
    links.push('<div><span style="color:var(--muted)">'+escHtml(name.toUpperCase())+':</span> '+escHtml(path)+'</div>');
  });
  adrJobSearchEl(prefix,'links').innerHTML=links.join('');
  adrJobSearchEl(prefix,'out').textContent=JSON.stringify(summary,null,2);
}
async function runAdrJobSearch(prefix='job-search'){
  const args=adrJobSearchArgs(prefix);
  adrJobSearchEl(prefix,'status').innerHTML='<span class="warn">Running…</span> Launching dedicated ADR tanker job search';
  adrJobSearchEl(prefix,'links').innerHTML='';
  adrJobSearchEl(prefix,'out').textContent='Calling run_job_search_tank_adr_improved…';
  try{
    const r=await apiFetch('/tools/call',{method:'POST',body:JSON.stringify({tool:'run_job_search_tank_adr_improved',arguments:args})});
    const d=await r.json();
    if(!r.ok || d.success===false){
      throw new Error(d.error||('HTTP '+r.status));
    }
    renderAdrJobSearchSummary(prefix,d.output||{});
  }catch(e){
    adrJobSearchEl(prefix,'status').innerHTML='<span class="fail">✗ Failed</span> '+escHtml(String(e));
    adrJobSearchEl(prefix,'out').textContent=String(e);
  }
}
async function runStructured(){
  const prompt=document.getElementById('so-prompt').value.trim();
  let schema={};try{schema=JSON.parse(document.getElementById('so-schema').value||'{}');}catch(e){alert('Invalid JSON schema');return;}
  try{
    const r=await apiFetch('/structured',{method:'POST',body:JSON.stringify({prompt,schema})});
    const d=await r.json();
    document.getElementById('so-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('so-out').textContent=String(e);}
}

// ── Sandbox ───────────────────────────────────────────────────────
async function runSandbox(){
  const code=document.getElementById('sb-code').value.trim();if(!code)return;
  const language=document.getElementById('sb-lang').value;
  const timeout=parseInt(document.getElementById('sb-timeout').value)||10;
  document.getElementById('sb-out').textContent='Executing…';
  try{
    const r=await apiFetch('/sandbox/run',{method:'POST',body:JSON.stringify({code,language})});
    const d=await r.json();
    const succ=d.success||d.status==='ok';
    document.getElementById('sb-out').innerHTML='<span class="'+(succ?'ok':'fail')+'">'+(succ?'✓ Success':'✗ Error')+'</span>\n\n'+escHtml(d.output||d.stdout||d.result||JSON.stringify(d,null,2))+(d.stderr?'\n\nSTDERR:\n'+escHtml(d.stderr):'');
    loadSandboxHistory();
  }catch(e){document.getElementById('sb-out').textContent=String(e);}
}
async function loadSandboxHistory(){
  try{
    const r=await apiFetch('/sandbox/history');const d=await r.json();
    const hist=d.history||[];
    document.getElementById('sb-hist').innerHTML=hist.length?
      hist.slice().reverse().map(h=>
        '<div style="border-bottom:1px solid var(--border);padding:6px 0;">'+
        '<div style="font-size:0.72rem;color:var(--muted)">'+(h.timestamp||'')+' · '+(h.language||'')+' · <span class="'+(h.success?'ok':'fail')+'">'+(h.success?'ok':'fail')+'</span></div>'+
        '<div class="out out-sm" style="margin-top:4px;">'+escHtml((h.code||'').substring(0,200))+((h.code||'').length>200?'…':'')+'</div>'+
        '</div>'
      ).join(''):
      '<span style="color:var(--muted);font-size:0.8rem">No execution history</span>';
  }catch(e){document.getElementById('sb-hist').textContent='unavailable';}
}

// ── KG ────────────────────────────────────────────────────────────
async function kgExtract(){
  const text=document.getElementById('kg-text').value.trim();if(!text)return;
  try{
    const r=await apiFetch('/kg/extract',{method:'POST',body:JSON.stringify({text})});
    const d=await r.json();
    document.getElementById('kg-extract-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('kg-extract-out').textContent=String(e);}
}
async function kgSearch(){
  const q=document.getElementById('kg-search').value.trim();
  try{
    const r=await apiFetch('/kg/search?name='+encodeURIComponent(q));
    const d=await r.json();
    document.getElementById('kg-search-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('kg-search-out').textContent=String(e);}
}
async function kgPath(){
  const from=document.getElementById('kg-from').value.trim();
  const to=document.getElementById('kg-to').value.trim();
  try{
    const r=await apiFetch('/kg/path?from='+encodeURIComponent(from)+'&to='+encodeURIComponent(to));
    const d=await r.json();
    document.getElementById('kg-search-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('kg-search-out').textContent=String(e);}
}
async function kgStats(){
  try{
    const r=await apiFetch('/kg/stats');const d=await r.json();
    document.getElementById('kg-search-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('kg-search-out').textContent='unavailable';}
}
async function kgExport(){
  try{
    const r=await apiFetch('/kg/export');const d=await r.json();
    document.getElementById('kg-export-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('kg-export-out').textContent=String(e);}
}

// ── Tools ─────────────────────────────────────────────────────────
async function loadTools(){
  try{
    const r=await apiFetch('/tools');const d=await r.json();
    const tools=d.tools||[];
    document.getElementById('tools-list').innerHTML=tools.length?
      '<table><thead><tr><th>Name</th><th>Description</th></tr></thead><tbody>'+
      tools.map(t=>'<tr><td style="color:var(--green)">'+(t.name||t.id)+'</td><td style="color:var(--muted);font-size:0.78rem">'+(t.description||'')+'</td></tr>').join('')+
      '</tbody></table>':
      '<span style="color:var(--muted);font-size:0.8rem">No tools registered</span>';
  }catch(e){document.getElementById('tools-list').textContent='unavailable';}
}
async function loadToolSchema(fmt){
  try{
    const r=await apiFetch('/tools?format='+fmt);const d=await r.json();
    document.getElementById('tools-schema-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('tools-schema-out').textContent=String(e);}
}
async function callTool(){
  const name=document.getElementById('tool-name').value.trim();if(!name)return;
  let params={};try{params=JSON.parse(document.getElementById('tool-params').value||'{}');}catch(e){alert('Invalid JSON');return;}
  try{
    const r=await apiFetch('/tools/call',{method:'POST',body:JSON.stringify({tool:name,arguments:params})});
    const d=await r.json();
    document.getElementById('tool-call-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('tool-call-out').textContent=String(e);}
}
async function runVision(){
  const image=document.getElementById('vis-url').value.trim();
  const prompt=document.getElementById('vis-prompt').value.trim();
  if(!image)return;
  try{
    const r=await apiFetch('/vision/analyze',{method:'POST',body:JSON.stringify({source:image,task:prompt})});
    const d=await r.json();
    document.getElementById('vis-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('vis-out').textContent=String(e);}
}
async function loadTracing(){
  try{
    const r=await apiFetch('/tracing/summary');const d=await r.json();
    document.getElementById('trace-out').innerHTML=Object.entries(d).map(([k,v])=>
      '<div class="stat"><span class="sl">'+k+'</span><span class="sv">'+JSON.stringify(v)+'</span></div>'
    ).join('')||'<span style="color:var(--muted)">No tracing data</span>';
  }catch(e){document.getElementById('trace-out').textContent='unavailable';}
}

// ── Models ────────────────────────────────────────────────────────
async function fetchModels(){
  try{
    const r=await apiFetch('/models');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    const tierCol={flagship:'bdg-g',balanced:'bdg-b',fast:'bdg-y',micro:'bdg-r'};
    const rows=(d.models||[]).map(m=>
      {
        const capabilities=Array.isArray(m.capabilities)
          ? m.capabilities
          : (typeof m.capability_count==='number'
            ? [String(m.capability_count)+' caps']
            : []);
        const bestFor=Array.isArray(m.best_for)
          ? m.best_for.join(', ')
          : (m.best_for||'');
        const contextLabel=typeof m.context_k==='number' ? (m.context_k+'k') : '—';
        return (
      '<tr>'+
      '<td style="color:var(--text)">'+m.id+'</td>'+
      '<td style="color:var(--muted)">'+m.provider+'</td>'+
      '<td><span class="bdg '+(tierCol[m.tier]||'bdg-b')+'">'+m.tier+'</span></td>'+
      '<td style="color:var(--blue);text-align:right">'+contextLabel+'</td>'+
      '<td style="color:var(--muted);font-size:0.75rem">'+bestFor+'</td>'+
      '<td style="font-size:0.72rem">'+capabilities.map(c=>'<span class="bdg bdg-p">'+c+'</span>').join(' ')+'</td>'+
      '</tr>'
        );
      }
    ).join('');
    document.getElementById('models-tbody').innerHTML=rows||'<tr><td colspan="6" style="color:var(--muted)">No models</td></tr>';
  }catch(e){
    document.getElementById('models-tbody').innerHTML='<tr><td colspan="6" style="color:var(--red)">Failed to load models: '+escHtml(String(e))+'</td></tr>';
  }
}
async function loadPersonas(){
  try{
    const r=await apiFetch('/personas');const d=await r.json();
    const ps=d.personas||[];
    document.getElementById('persona-list').innerHTML=ps.length?
      ps.map(p=>'<div class="stat"><span class="sl">'+(p.id||p.name)+'</span><span class="sv" style="color:var(--muted);font-size:0.75rem">'+((p.description||'').substring(0,50))+'</span></div>').join(''):
      '<span style="color:var(--muted);font-size:0.8rem">No personas defined</span>';
  }catch(e){document.getElementById('persona-list').textContent='unavailable';}
}
async function setPersona(){
  const id=document.getElementById('persona-set').value.trim();if(!id)return;
  const sid=document.getElementById('chat-session').value||'demo';
  try{
    const r=await apiFetch('/personas/session/'+encodeURIComponent(sid),{method:'POST',body:JSON.stringify({persona:id})});
    const d=await r.json();
    document.getElementById('persona-out').textContent=JSON.stringify(d,null,2);
  }catch(e){document.getElementById('persona-out').textContent=String(e);}
}
async function loadTemplates(){
  try{
    const r=await apiFetch('/templates');const d=await r.json();
    const ts=d.templates||[];
    document.getElementById('tmpl-list').innerHTML=ts.length?
      ts.map(t=>'<div class="stat"><span class="sl">'+(t.name||t.id)+'</span><span class="sv info">'+(t.variables||0)+' vars</span></div>').join(''):
      '<span style="color:var(--muted);font-size:0.8rem">No templates</span>';
  }catch(e){document.getElementById('tmpl-list').textContent='unavailable';}
}
async function loadMemories(){
  try{
    const r=await apiFetch('/memories');const d=await r.json();
    const ms=d.memories||[];
    document.getElementById('mem-list').innerHTML=ms.length?
      ms.slice(0,10).map(m=>'<div class="stat"><span class="sl" style="font-size:0.75rem;max-width:200px;overflow:hidden;text-overflow:ellipsis">'+escHtml((m.content||'').substring(0,60))+'</span><span class="sv" style="color:var(--muted);font-size:0.72rem">'+(m.type||'')+'</span></div>').join(''):
      '<span style="color:var(--muted);font-size:0.8rem">No memories stored</span>';
  }catch(e){document.getElementById('mem-list').textContent='unavailable';}
}

// ── Utils ─────────────────────────────────────────────────────────
function escHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function escAttr(s){return escHtml(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

// ── Boot ──────────────────────────────────────────────────────────
initOverview();
setInterval(initOverview,20000);
</script>
</body>
</html>"""


def register_dashboard(app: web.Application, agent: "OmniAgent"):
    """Register dashboard routes on an existing aiohttp app."""

    async def dashboard(request):
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def audit_endpoint(request):
        limit = int(request.rel_url.query.get("limit", "30"))
        log = agent.memory.get_audit_log(limit=limit)
        return web.json_response({"log": log})

    app.router.add_get("/dashboard", dashboard)
    app.router.add_get("/", dashboard)
    app.router.add_get("/audit", audit_endpoint)
    logger.info("Dashboard registered at /dashboard")
