const NS='astrbot_persona_studio';
const split=value=>value.split(/[、,，\r\n]+/).map(v=>v.trim()).filter(Boolean);
const unique=values=>[...new Set(values)];

export class WorldbookEditor {
  constructor(state,{escape,icon,post,changed,applyDisabled}) {
    Object.assign(this,{state,escape,icon,post,changed,applyDisabled});
    this.reset();
  }
  reset(preserveView=false) {
    clearTimeout(this.timer);this.revision=(this.revision||0)+1;
    this.drafts={};this.numericEdits=[];this.result=null;this.composing=false;
    if(!preserveView){this.selected=0;this.test='';this.history='';this.username='访客';}
  }
  book(create=false) {
    const data=this.state.document.data;
    if(create&&!data.character_book)data.character_book={entries:[],extensions:{}};
    return data.character_book||{entries:[]};
  }
  settings(create=false) {
    const book=this.book(create);
    if(create){
      book.extensions??={};
      if(typeof book.extensions!=='object'||Array.isArray(book.extensions))throw Error('导入世界书的 extensions 格式无效，请先在原始数据中调整。');
      book.extensions[NS]??={};
      if(typeof book.extensions[NS]!=='object'||Array.isArray(book.extensions[NS]))throw Error('世界书运行设置格式无效，请先在原始数据中调整。');
    }
    const value=book.extensions?.[NS];
    return value&&typeof value==='object'&&!Array.isArray(value)?value:{};
  }
  writable() {const c=this.state.current;return c&&!c.locked&&!c.deleted&&!c.sync.core_locked;}
  editable() {return this.writable()&&!this.state.busy;}
  entries() {return this.book().entries||[];}
  entry() {const e=this.entries()[this.selected];return e&&typeof e==='object'&&!Array.isArray(e)?e:null;}
  hasDraft() {return Object.values(this.drafts).some(v=>v.trim());}
  draft(document) {
    if(!this.hasDraft())return document;
    const copy=structuredClone(document), entry=copy.data.character_book?.entries?.[this.selected];
    if(entry&&typeof entry==='object')for(const [key,value] of Object.entries(this.drafts))entry[key]=unique([...(Array.isArray(entry[key])?entry[key]:[]),...split(value)]);
    return copy;
  }
  flush() {
    const e=this.entry();
    if(e&&this.writable())for(const [key,value] of Object.entries(this.drafts)){
      if(value.trim())e[key]=unique([...(Array.isArray(e[key])?e[key]:[]),...split(value)]);
    }
    this.drafts={};
  }
  validate() {
    if(this.state.raw!==null)return;
    for(const {target,key,min,max} of this.numericEdits){
      if(target!==this.book()&&target!==this.settings()&&!this.entries().includes(target))continue;
      const value=target[key];
      if(!Number.isInteger(value)||value<min||value>max)throw Error(`世界书数值须为 ${min}～${max} 之间的整数，请在世界书页签调整。`);
    }
  }
  name(entry,index) {return entry?.name||entry?.comment||`条目 ${index+1}`;}
  chips(key,label,entry) {
    const esc=this.escape, values=Array.isArray(entry[key])?entry[key]:[];
    return `<div class="field"><label class="field-label" for="wb-key-${key}">${label}</label><div class="tag-editor wb-key-editor"><div class="tag-chip-list">${values.map((v,i)=>`<span class="tag-chip"><span>${esc(v)}</span><button type="button" data-wb-action="remove-key" data-key="${key}" data-index="${i}" aria-label="移除关键词：${esc(v)}">${this.icon('x')}</button></span>`).join('')}</div><input id="wb-key-${key}" data-wb-key="${key}" value="${esc(this.drafts[key]||'')}" placeholder="输入关键词…" autocomplete="off" enterkeyhint="done"><button type="button" class="tag-add" data-wb-action="add-key" data-key="${key}">${this.icon('plus')}添加</button></div><span class="field-help">按 Enter 添加，多个关键词匹配任意一个即可。</span></div>`;
  }
  renderList() {
    const esc=this.escape, entries=this.entries();
    return `<div class="wb-list-heading"><span>${entries.length} 条设定</span><button class="quiet" data-wb-action="add" ${entries.length>=500?'disabled':''}>${this.icon('plus')}添加</button></div><div class="wb-entry-list" role="group" aria-label="世界书条目">${entries.slice(0,500).map((e,i)=>`<button data-wb-select="${i}" aria-pressed="${this.selected===i}"><span class="wb-entry-title">${esc(this.name(e,i))}</span><span class="wb-entry-caption">${esc(e?.enabled===false?'已停用':e?.constant?'始终生效':Array.isArray(e?.keys)&&e.keys.length?e.keys.map(v=>String(v)).join(' · '):'等待添加关键词')}</span></button>`).join('')||'<p class="small muted wb-empty-list">还没有条目，先添加一段世界背景。</p>'}</div>`;
  }
  renderEntry() {
    const esc=this.escape, e=this.entry();
    if(!this.entries().length)return `<div class="wb-empty"><span class="wb-empty-icon">${this.icon('book')}</span><h3>让角色拥有自己的世界</h3><p class="small muted">人物关系、地点、组织与历史，都可以成为一条设定。</p><button data-wb-action="add">${this.icon('plus')}添加第一条设定</button></div>`;
    if(!e)return '<div class="notice">这条导入数据格式不完整，已原样保留。请在“更多设置 → 原始数据”中调整。</div>';
    const position=e.position??(e.extensions?.position===0?'before_char':'after_char');
    return `<div class="wb-entry-toolbar"><span class="eyebrow">WORLD ENTRY ${String(this.selected+1).padStart(2,'0')}</span><label class="checkbox-label"><input type="checkbox" data-wb-entry="enabled" ${e.enabled!==false?'checked':''}>启用条目</label></div><div class="form"><label class="field"><span class="field-label">条目名称</span><input data-wb-entry="name" value="${esc(e.name||e.comment||'')}" placeholder="例如：白塔法师协会" maxlength="255"></label><label class="field"><span class="field-label">触发方式</span><select data-wb-entry="constant"><option value="false" ${!e.constant?'selected':''}>关键词触发</option><option value="true" ${e.constant?'selected':''}>始终生效</option></select><span class="field-help">${e.constant?'每轮对话都尝试注入，仍受字符预算限制。':'扫描当前消息及设定范围内的历史，按文字包含关系匹配。'}</span></label>${!e.constant?`${this.chips('keys','触发关键词',e)}<label class="checkbox-label"><input type="checkbox" data-wb-entry="case_sensitive" ${(e.case_sensitive??e.extensions?.case_sensitive)===true?'checked':''}>区分大小写</label><details ${e.selective?'open':''}><summary>辅助触发条件</summary><div class="form"><label class="checkbox-label"><input type="checkbox" data-wb-entry="selective" ${e.selective?'checked':''}>同时命中任一辅助关键词才触发</label>${this.chips('secondary_keys','辅助关键词',e)}</div></details>`:''}<label class="field"><span class="field-label">设定正文</span><textarea data-wb-entry="content" rows="7" placeholder="写下触发时提供给角色的背景设定…">${esc(e.content||'')}</textarea><span class="field-help">支持 {{char}} 与 {{user}}，不会将这段正文作为下一条设定的触发来源。</span></label><div class="two-columns"><label class="field"><span class="field-label">注入位置</span><select data-wb-entry="position"><option value="before_char" ${position==='before_char'?'selected':''}>角色设定前</option><option value="after_char" ${position==='after_char'?'selected':''}>角色设定后</option>${!['before_char','after_char'].includes(position)?`<option selected value="${esc(position)}">导入的位置（暂不支持）</option>`:''}</select></label><label class="field"><span class="field-label">预算优先级</span><input type="number" data-wb-entry="priority" min="-10000" max="10000" step="1" value="${Number.isInteger(e.priority)?e.priority:0}"><span class="field-help">数值越大，预算不足时越优先保留。</span></label></div><details><summary>排列与兼容说明</summary><div class="form"><label class="field"><span class="field-label">注入顺序</span><input type="number" data-wb-entry="insertion_order" min="-1000000" max="1000000" step="1" value="${Number.isInteger(e.insertion_order)?e.insertion_order:100}"><span class="field-help">同一位置按数值从小到大排列；相同时按列表顺序。</span></label><p class="small muted">导入的其他字段原样保存。触发预览会说明未支持的规则，可在原始数据中调整。</p></div></details><div class="wb-entry-footer"><button class="quiet danger" data-wb-action="remove">移除此条设定</button><span class="small muted">保存后随角色卡一起生效</span></div></div>`;
  }
  render() {
    if(this.state.tab!=='worldbook')return;
    this.selected=Math.min(Math.max(0,this.selected),Math.max(0,Math.min(this.entries().length,500)-1));
    const esc=this.escape, book=this.book(), settings=this.settings();
    const depth=Number.isInteger(book.scan_depth)&&book.scan_depth>=0&&book.scan_depth<=50?book.scan_depth:4;
    const budget=Number.isInteger(settings.max_chars)&&settings.max_chars>=0&&settings.max_chars<=16000?settings.max_chars:4000;
    document.querySelector('#panel').innerHTML=`<div class="panel-heading"><div><h3>世界书</h3><p class="small muted">在故事需要的时候，补充恰到好处的背景。</p></div><label class="checkbox-label"><input type="checkbox" data-wb-book="enabled" ${settings.enabled!==false?'checked':''}>启用世界书</label></div><div class="wb-settings"><label class="field"><span class="field-label">世界书名称</span><input data-wb-book="name" value="${esc(book.name||'')}" placeholder="这个世界的名字"></label><label class="field"><span class="field-label">扫描历史消息</span><input type="number" data-wb-book="scan_depth" min="0" max="50" step="1" value="${depth}"><span class="field-help">最近 0～50 条，含用户与助手</span></label><label class="field"><span class="field-label">注入预算 · 字符</span><input type="number" data-wb-book="max_chars" min="0" max="16000" step="1" value="${budget}"><span class="field-help">0～16000，0 表示不注入</span></label></div><p class="small muted wb-scan-help">始终扫描当前消息；临时提示词、示例和工具结果不参与匹配。超出预算的条目整条跳过。</p><div class="wb-layout"><nav class="wb-list" aria-label="世界书条目导航">${this.renderList()}</nav><section class="wb-entry" aria-label="条目编辑">${this.renderEntry()}</section></div><section class="wb-test-section" aria-labelledby="wb-test-title"><div class="panel-heading"><div><h3 id="wb-test-title">触发预览</h3><p class="small muted">使用当前草稿测试，无需先保存。</p></div><button class="quiet" data-wb-action="test">${this.icon('refresh')}重新测试</button></div><label class="field"><span class="field-label">测试消息</span><textarea data-wb-test="test" rows="2" placeholder="例如：今天我们去白塔看看吧。">${esc(this.test)}</textarea></label><details class="wb-test-options"><summary>添加测试历史与用户名</summary><div class="form"><label class="field"><span class="field-label">历史消息 · 从旧到新，每行一条</span><textarea data-wb-test="history" rows="3">${esc(this.history)}</textarea><span class="field-help">最多 50 条，仅按上方扫描范围取最近几条。</span></label><label class="field"><span class="field-label">测试用户名</span><input data-wb-test="username" maxlength="255" value="${esc(this.username)}"></label></div></details><div id="wb-result" role="status" aria-live="polite"></div></section>`;
    this.applyDisabled();this.schedule();
  }
  refreshList() {
    const list=document.querySelector('.wb-list');if(list)list.innerHTML=this.renderList();
    this.applyDisabled();
  }
  input(event) {
    const el=event.target;
    if(el.dataset.wbTest){this[el.dataset.wbTest]=el.value;this.schedule();return true;}
    if(!el.dataset.wbEntry&&!el.dataset.wbBook&&!el.dataset.wbKey)return false;
    if(!this.editable())return true;
    if(el.dataset.wbKey){
      this.drafts[el.dataset.wbKey]=el.value;
      if(!this.composing&&!event.isComposing&&/[、,，\r\n]/.test(el.value)){this.flush();this.render();document.querySelector(`[data-wb-key="${el.dataset.wbKey}"]`)?.focus();}
    }else if(el.dataset.wbBook){
      const key=el.dataset.wbBook;
      const target=key==='enabled'||key==='max_chars'?this.settings(true):this.book(true);
      target[key]=key==='enabled'?el.checked:el.type==='number'?(el.value===''?null:Number(el.value)):el.value;
      if(el.type==='number')this.numericEdits.push({target,key,min:Number(el.min),max:Number(el.max)});
    }else{
      const key=el.dataset.wbEntry,e=this.entry();if(!e)return true;
      e[key]=el.type==='checkbox'?el.checked:el.type==='number'?(el.value===''?null:Number(el.value)):key==='constant'?el.value==='true':el.value;
      if(el.type==='number')this.numericEdits.push({target:e,key,min:Number(el.min),max:Number(el.max)});
      if(key==='position'&&e.extensions&&Object.hasOwn(e.extensions,'position'))e.extensions.position=e.position==='before_char'?0:1;
      if(key==='constant'){this.flush();this.render();document.querySelector('[data-wb-entry="constant"]')?.focus();}
    }
    this.refreshList();this.changed();this.schedule();return true;
  }
  keydown(event) {
    const key=event.target.dataset.wbKey;if(!key)return false;
    if(this.composing||event.isComposing||event.keyCode===229||!this.editable())return true;
    if(['Enter',',','，','、'].includes(event.key)){
      event.preventDefault();this.flush();this.render();this.changed();document.querySelector(`[data-wb-key="${key}"]`)?.focus();
    }else if(event.key==='Backspace'&&!event.target.value&&Array.isArray(this.entry()?.[key])){
      this.entry()[key].pop();this.render();this.changed();document.querySelector(`[data-wb-key="${key}"]`)?.focus();
    }
    return true;
  }
  composition(event) {
    if(!event.target.dataset.wbKey)return false;
    this.composing=event.type==='compositionstart';
    if(!this.composing)this.input(event);
    return true;
  }
  paste(event) {
    const el=event.target,key=el.dataset.wbKey;
    if(!key||!this.editable())return false;
    const text=event.clipboardData?.getData('text/plain');
    if(!text||!/[、,，\r\n]/.test(text))return false;
    event.preventDefault();
    const start=el.selectionStart??el.value.length,end=el.selectionEnd??start;
    this.drafts[key]=el.value.slice(0,start)+text+el.value.slice(end);
    this.flush();this.render();this.changed();document.querySelector(`[data-wb-key="${key}"]`)?.focus();
    return true;
  }
  click(button) {
    const action=button.dataset.wbAction;
    if(button.dataset.wbSelect!==undefined){this.flush();this.selected=Number(button.dataset.wbSelect);this.render();this.changed();return;}
    if(action==='test'){this.schedule(0);return;}
    if(!this.editable())return;
    this.flush();
    if(action==='add'){
      const book=this.book(true);book.entries??=[];if(book.entries.length>=500)return;
      book.entries.push({name:'新设定',keys:[],content:'',enabled:true,constant:false,use_regex:false,insertion_order:100,position:'after_char',extensions:{}});
      this.selected=book.entries.length-1;
    }
    if(action==='remove'){this.entries().splice(this.selected,1);this.selected=Math.max(0,this.selected-1);}
    if(action==='remove-key'&&Array.isArray(this.entry()?.[button.dataset.key]))this.entry()[button.dataset.key].splice(Number(button.dataset.index),1);
    this.render();this.changed();
    if(action==='add')document.querySelector('[data-wb-entry="name"]')?.focus();
    if(action==='add-key'||action==='remove-key')document.querySelector(`[data-wb-key="${button.dataset.key}"]`)?.focus();
  }
  schedule(delay=300) {
    clearTimeout(this.timer);const revision=++this.revision;
    if(this.state.tab!=='worldbook'||!document.querySelector('#wb-result'))return;
    const output=document.querySelector('#wb-result');output.setAttribute('aria-busy','true');
    if(!output.textContent)output.innerHTML='<p class="small muted">正在计算触发结果…</p>';
    this.timer=setTimeout(async()=>{
      try{
        const result=await this.post('worldbook_preview',{card:this.draft(this.state.document),prompt:this.test,history:this.history.split('\n').filter(v=>v.trim()),username:this.username||'访客'});
        if(revision!==this.revision||this.state.tab!=='worldbook')return;
        this.result=result;this.renderResult();
      }catch(error){if(revision===this.revision&&document.querySelector('#wb-result')){document.querySelector('#wb-result').textContent=error.message||String(error);document.querySelector('#wb-result').setAttribute('aria-busy','false');}}
    },delay);
  }
  renderResult() {
    const el=document.querySelector('#wb-result'), r=this.result, esc=this.escape;if(!el||!r)return;
    const included=r.entries.filter(e=>e.status==='included').length;
    const labels={included:'已注入',unmatched:'未触发',disabled:'已停用',book_disabled:'已停用',budget:'预算不足',unsupported:'暂不支持',invalid:'格式无效',empty:'正文为空'};
    el.setAttribute('aria-busy','false');
    el.innerHTML=`<div class="wb-result-summary"><span><strong>${included}</strong> 条生效 <span class="muted">/ ${r.entries.length} 条设定</span></span><span class="small muted">${r.used_chars} / ${r.max_chars} 字符 · 扫描 ${r.scanned_messages} 条消息</span></div>${!r.enabled?'<p class="small warning">世界书已停用，本轮不会注入内容。</p>':''}${r.scan_truncated?'<p class="small warning">扫描文本超过 32000 字符，已优先保留最新内容。</p>':''}<div class="wb-result-list">${r.entries.map(e=>`<div class="wb-result-row"><span class="wb-result-name">${esc(e.name)}</span><span class="tag ${e.status==='included'?'ok':['budget','invalid','unsupported'].includes(e.status)?'warning':''}">${labels[e.status]||esc(e.status)}</span><span class="small muted">${esc(e.reason)}</span></div>`).join('')||'<p class="small muted">添加设定后，这里会显示每一条的触发原因。</p>'}</div>${r.before?`<details class="wb-injection" open><summary>角色设定前 · 实际注入内容</summary><pre>${esc(r.before)}</pre></details>`:''}${r.after?`<details class="wb-injection" open><summary>角色设定后 · 实际注入内容</summary><pre>${esc(r.after)}</pre></details>`:''}${r.warnings.length?`<details class="wb-compatibility"><summary>${r.warnings.length} 项兼容说明</summary>${r.warnings.map(w=>`<p class="small warning">${esc(w)}</p>`).join('')}</details>`:''}`;
  }
}
