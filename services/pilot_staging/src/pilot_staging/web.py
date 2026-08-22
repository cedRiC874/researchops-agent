from __future__ import annotations


PILOT_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ResearchOps 外部科研用户 Pilot</title>
  <link rel="stylesheet" href="/pilot/style.css">
</head>
<body>
  <main class="shell">
    <header>
      <p class="eyebrow">INVITE-ONLY RESEARCH PILOT</p>
      <h1>ResearchOps 科研用户体验测试</h1>
      <p class="lede">你只需判断回答是否易懂、是否有助于下一步；不需要判断专业结论是否正确。</p>
      <p id="mode-banner" class="message" hidden></p>
    </header>
    <section id="message" class="message" hidden></section>
    <section id="login" class="card">
      <h2>使用邀请进入</h2>
      <label>邀请令牌 <input id="invite" type="password" autocomplete="off" maxlength="256"></label>
      <p class="hint">令牌只用于换取本浏览器的安全会话，不会发送给模型。</p>
      <button id="login-button" type="button">进入 Pilot</button>
    </section>
    <section id="consent" class="card" hidden>
      <h2>参加前确认</h2>
      <div id="consent-document" class="markdown"></div>
      <div id="consent-items"></div>
      <button id="consent-button" type="button">我同意并开始</button>
    </section>
    <section id="task" hidden>
      <div class="progress"><span id="sequence"></span><span id="session-id"></span></div>
      <article class="card prompt-card">
        <p class="eyebrow">TASK / 题目</p>
        <h2>English</h2><div id="prompt-en" class="markdown"></div>
        <h2>中文</h2><div id="prompt-zh" class="markdown"></div>
      </article>
      <div id="reveal-panel" class="card">
        <p>点击后系统才会运行并显示答案。答案显示时，人工阅读计时开始。</p>
        <button id="reveal-button" type="button">查看答案</button>
        <button id="skip-task-button" class="link-button" type="button">跳过本题</button>
        <button id="skip-button" type="button" hidden>记录技术故障并进入下一题</button>
        <span id="provider-status" class="hint"></span>
      </div>
      <article id="answer-card" class="card answer-card" hidden>
        <div class="answer-heading"><p class="eyebrow">AGENT OUTPUT / 答案</p><span id="timer">00:00</span></div>
        <div id="answer" class="markdown"></div>
        <button id="skip-after-answer" class="link-button" type="button">不评价并跳过本题</button>
      </article>
      <form id="feedback" class="card" hidden>
        <h2>你的使用体验</h2>
        <p class="hint">请按你的实际感受回答；这里不要求你验证统计或领域正确性。</p>
        <div id="feedback-fields"></div>
        <label class="notes">补充说明（通常可选；低把握或需要专家复核时必填）<textarea id="notes" maxlength="2000" rows="5"></textarea></label>
        <button type="submit">提交并进入下一题</button>
      </form>
    </section>
    <section id="complete" class="card" hidden><h2>已完成</h2><p>感谢参与。你的反馈将只按去标识化聚合口径报告。</p></section>
    <footer><button id="withdraw" class="link-button" type="button" hidden>退出本次 Pilot</button></footer>
  </main>
  <script src="/pilot/app.js" defer></script>
</body>
</html>
"""


PILOT_CSS = """:root{color-scheme:light;--ink:#17201d;--muted:#64706b;--paper:#f4f1e8;--card:#fffdf8;--accent:#135f4b;--line:#d8d4c8;--danger:#9b2c2c}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#eef4ed,var(--paper) 46%,#eee8da);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}.shell{width:min(900px,calc(100% - 32px));margin:48px auto 80px}header{margin-bottom:28px}h1{font:700 clamp(2rem,6vw,4rem)/1.04 Georgia,serif;letter-spacing:-.035em;margin:.2rem 0 1rem}.lede{font-size:1.1rem;max-width:700px}.eyebrow{font-size:.76rem;font-weight:800;letter-spacing:.14em;color:var(--accent);margin:0 0 .5rem}.card{background:color-mix(in srgb,var(--card) 94%,transparent);border:1px solid var(--line);border-radius:18px;padding:clamp(20px,4vw,36px);margin:18px 0;box-shadow:0 12px 40px #25442b12}.prompt-card{border-left:6px solid var(--accent)}.answer-card{border-left:6px solid #b36b16}.progress,.answer-heading{display:flex;justify-content:space-between;gap:16px;align-items:center;color:var(--muted);font-size:.9rem}.answer-heading .eyebrow{margin:0}.markdown{overflow-wrap:anywhere}.markdown pre{overflow:auto;background:#19221f;color:#f4f1e8;padding:16px;border-radius:10px}.markdown code{background:#e9ece6;padding:.1em .3em;border-radius:4px}.markdown pre code{background:transparent;padding:0}.markdown table{border-collapse:collapse;display:block;overflow:auto}.markdown td,.markdown th{border:1px solid var(--line);padding:.4rem .7rem}label{display:block;margin:14px 0;font-weight:650}input,textarea{width:100%;margin-top:6px;border:1px solid #b8bdb7;border-radius:8px;padding:11px;background:white;color:var(--ink);font:inherit}button{border:0;border-radius:999px;background:var(--accent);color:white;font-weight:750;padding:11px 20px;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.hint{color:var(--muted);font-size:.9rem}.message{padding:12px 16px;background:#fff5d9;border:1px solid #ddb85a;border-radius:10px}.choice{border-top:1px solid var(--line);padding:14px 0}.choice legend{font-weight:700}.choice label{display:inline-flex;align-items:center;margin:7px 18px 0 0;font-weight:500}.choice input{width:auto;margin:0 7px 0 0}.notes{border-top:1px solid var(--line);padding-top:18px}.link-button{background:transparent;color:var(--danger);text-decoration:underline;padding-left:0}@media(max-width:600px){.shell{margin-top:24px}.progress,.answer-heading{align-items:flex-start;flex-direction:column}}
"""


PILOT_JS = r"""'use strict';
let csrf = null;
let currentAttempt = null;
let timerHandle = null;
let timerStarted = null;
const $ = (id) => document.getElementById(id);
const show = (id, value=true) => { $(id).hidden = !value; };
function cookie(name){const prefix=name+'=';for(const item of document.cookie.split(';')){const value=item.trim();if(value.startsWith(prefix))return decodeURIComponent(value.slice(prefix.length));}return null;}
function message(text){ $('message').textContent=text; show('message',Boolean(text)); }
function setMode(supervised){$('mode-banner').textContent=supervised?'监督式预试运行：本场只用于发现操作与流程问题，不构成正式外部验证。':'';show('mode-banner',Boolean(supervised));}
function escapeHtml(text){return text.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function markdown(text){
  const escaped=escapeHtml(text||''); const lines=escaped.split(/\r?\n/); let html=''; let inCode=false; let list=false;
  const inline=(s)=>s.replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>');
  for(const raw of lines){
    if(raw.trim().startsWith('```')){if(list){html+='</ul>';list=false;} html+=inCode?'</code></pre>':'<pre><code>';inCode=!inCode;continue;}
    if(inCode){html+=raw+'\n';continue;}
    const heading=raw.match(/^(#{1,4})\s+(.+)$/); if(heading){if(list){html+='</ul>';list=false;} const n=heading[1].length+2;html+=`<h${n}>${inline(heading[2])}</h${n}>`;continue;}
    const bullet=raw.match(/^\s*[-*]\s+(.+)$/); if(bullet){if(!list){html+='<ul>';list=true;} html+='<li>'+inline(bullet[1])+'</li>';continue;}
    if(list){html+='</ul>';list=false;} if(raw.trim())html+='<p>'+inline(raw)+'</p>';
  }
  if(list)html+='</ul>'; if(inCode)html+='</code></pre>'; return html;
}
async function api(path, options={}){
  const headers={'Content-Type':'application/json',...(options.headers||{})}; if(csrf)headers['X-CSRF-Token']=csrf;
  const response=await fetch(path,{credentials:'same-origin',...options,headers}); let body={}; try{body=await response.json();}catch(_){}
  if(!response.ok){const code=body.error_code||body.detail?.error_code||`http_${response.status}`;throw new Error(code);} return body;
}
const consentItems={
 adult_and_voluntary:'我已满 18 岁，并自愿参加。',
 experimental_system_understood:'我知道这是可能出错的实验性 AI 原型，结果不能直接用于决策或发表。',
 public_data_only:'我只使用页面提供的公开数据，不输入个人、患者、机构或保密数据。',
 provider_transfer_understood:'我知道题目和聚合工具结果会发送给页面所述的模型 Provider。',
 pseudonymous_recording_agreed:'我同意记录假名化任务状态、计时、反馈和安全事件。',
 withdrawal_understood:'我知道可以随时退出，退出会阻止后续模型调用。',
 external_researcher_eligible:'我不是本项目作者、开发者或贡献者，也没有看过这些任务的标准答案。'
};
function buildConsent(){const root=$('consent-items');root.textContent='';for(const [key,label] of Object.entries(consentItems)){const el=document.createElement('label');const input=document.createElement('input');input.type='checkbox';input.dataset.key=key;input.style.width='auto';input.style.marginRight='8px';el.append(input,document.createTextNode(label));root.append(el);}}
const fields=[
 ['understandable','回答是否容易理解？'],['useful_for_next_step','回答是否有助于你决定下一步？'],
 ['confidence','你对自己的这次评价有多大把握？'],['needs_expert_review','你是否希望领域或统计专家进一步检查？'],
 ['obvious_problem','你是否看到了明显问题？'],['missing_information','回答是否缺少你继续工作所需的信息？'],
 ['safety_concern','你是否担心泄露、越权或审批绕过？']
];
function choice(name,label,values){const set=document.createElement('fieldset');set.className='choice';const legend=document.createElement('legend');legend.textContent=label;set.append(legend);for(const [value,text] of values){const lab=document.createElement('label');const input=document.createElement('input');input.type='radio';input.name=name;input.value=value;input.required=true;lab.append(input,document.createTextNode(text));set.append(lab);}return set;}
function buildFeedback(clarification){const root=$('feedback-fields');root.textContent='';for(const [name,label] of fields){root.append(choice(name,label,name==='confidence'?[['low','低'],['medium','中'],['high','高']]:[['true','是'],['false','否']]));}if(clarification)root.append(choice('clarification_useful','这次澄清是否合理且有帮助？',[['true','是'],['false','否']]));}
async function loadState(){
  const data=await api('/v1/pilot/state',{method:'GET'}); message('');setMode(Boolean(data.supervised_pretest));
  if(data.status==='consent_required'){$('consent-document').innerHTML=markdown(data.consent_document);show('login',false);show('consent');show('withdraw');return;}
  if(data.status==='complete'){show('task',false);show('complete');show('withdraw');return;}
  const a=data.attempt;currentAttempt=a;$('sequence').textContent=`第 ${a.sequence} / ${a.task_count} 题`;$('session-id').textContent=data.session_instance_id;
  $('prompt-en').innerHTML=markdown(a.task.prompt_en);$('prompt-zh').innerHTML=markdown(a.task.prompt_zh);show('login',false);show('consent',false);show('complete',false);show('task');show('withdraw');
  if(a.agent_output){displayAnswer(a);}else{show('answer-card',false);show('feedback',false);show('skip-after-answer',false);show('reveal-panel');const failed=a.status==='failed'||a.status==='withheld';show('reveal-button',!failed);show('skip-task-button',!failed&&a.status==='assigned');show('skip-button',failed);$('reveal-button').disabled=false;$('provider-status').textContent=failed?'本题未产生可评价答案；该运行不会计入可用性分母。':(a.status==='queued'||a.status==='running'?'答案正在生成…':'');}
}
function displayAnswer(a){show('reveal-panel',false);show('answer-card');show('skip-after-answer');show('feedback');$('answer').innerHTML=markdown(a.agent_output);buildFeedback(a.clarification_feedback_required);timerStarted=Date.now();if(timerHandle)clearInterval(timerHandle);timerHandle=setInterval(()=>{const s=Math.floor((Date.now()-timerStarted)/1000);$('timer').textContent=`${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;},1000);}
async function reveal(){try{$('reveal-button').disabled=true;const data=await api(`/v1/pilot/attempts/${currentAttempt.attempt_id}/reveal`,{method:'POST',body:'{}'});if(data.status==='revealed'){currentAttempt=data.attempt;displayAnswer(data.attempt);return;}$('provider-status').textContent='答案正在生成…';setTimeout(pollAttempt,1500);}catch(e){message(`无法显示答案：${e.message}`);$('reveal-button').disabled=false;}}
async function pollAttempt(){try{const data=await api(`/v1/pilot/attempts/${currentAttempt.attempt_id}`,{method:'GET'});currentAttempt=data.attempt;if(data.attempt.answer_available){await reveal();return;}if(data.attempt.status==='failed'||data.attempt.status==='withheld'){await loadState();return;}setTimeout(pollAttempt,1500);}catch(e){message(`查询答案状态失败：${e.message}`);$('reveal-button').disabled=false;}}
async function skipFailure(){try{await api(`/v1/pilot/attempts/${currentAttempt.attempt_id}/exclude`,{method:'POST',body:'{}'});await loadState();}catch(e){message(`无法继续：${e.message}`);}}
async function skipTask(){if(!confirm('确认跳过本题？本题不会计入可用性反馈，且不能重跑。'))return;try{await api(`/v1/pilot/attempts/${currentAttempt.attempt_id}/skip`,{method:'POST',body:'{}'});if(timerHandle)clearInterval(timerHandle);await loadState();}catch(e){message(`无法跳过：${e.message}`);}}
async function login(){try{const token=$('invite').value.trim();const data=await api('/v1/pilot/auth/session',{method:'POST',body:JSON.stringify({invite_token:token})});csrf=data.csrf_token;setMode(Boolean(data.campaign.supervised_pretest));$('consent-document').innerHTML=markdown(data.consent_document);$('invite').value='';await loadState();}catch(e){message(`无法进入：${e.message}`);}}
async function consent(){try{const payload={};for(const input of document.querySelectorAll('#consent-items input'))payload[input.dataset.key]=input.checked;await api('/v1/pilot/consent',{method:'POST',body:JSON.stringify(payload)});await loadState();}catch(e){message(`无法记录同意：${e.message}`);}}
async function feedback(event){event.preventDefault();try{const form=new FormData(event.target);const payload={};for(const [name] of fields){const value=form.get(name);payload[name]=name==='confidence'?value:value==='true';}if(currentAttempt.clarification_feedback_required)payload.clarification_useful=form.get('clarification_useful')==='true';payload.notes=$('notes').value;await api(`/v1/pilot/attempts/${currentAttempt.attempt_id}/feedback`,{method:'POST',body:JSON.stringify(payload)});if(timerHandle)clearInterval(timerHandle);$('notes').value='';await loadState();}catch(e){message(`反馈未保存：${e.message}`);}}
async function withdraw(){if(!confirm('确认退出？退出后本邀请不能再次使用，也不会再发起模型调用。'))return;try{await api('/v1/pilot/withdraw',{method:'POST',body:'{}'});csrf=null;location.reload();}catch(e){message(`退出失败：${e.message}`);}}
document.addEventListener('DOMContentLoaded',async()=>{buildConsent();$('login-button').addEventListener('click',login);$('consent-button').addEventListener('click',consent);$('reveal-button').addEventListener('click',reveal);$('skip-task-button').addEventListener('click',skipTask);$('skip-after-answer').addEventListener('click',skipTask);$('skip-button').addEventListener('click',skipFailure);$('feedback').addEventListener('submit',feedback);$('withdraw').addEventListener('click',withdraw);const hash=new URLSearchParams(location.hash.slice(1));if(hash.get('invite')){$('invite').value=hash.get('invite');history.replaceState(null,'',location.pathname);}csrf=cookie('researchops_pilot_csrf');if(csrf){try{await loadState();}catch(_){csrf=null;}}});
"""
