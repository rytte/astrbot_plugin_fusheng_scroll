import {WorldbookEditor} from './worldbook.js';

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (selector) => document.querySelector(selector);
const clone = (value) => JSON.parse(JSON.stringify(value));
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
const state = {items:[], activeCount:0, current:null, document:null, options:null, tab:'character', busy:false, raw:null, tagDraft:'', showKeys:false, avatar:null, avatarWarning:null, cardImage:null, cardImageWarning:null, pendingImages:{}, deleted:false, previewOpen:false, view:'collection'};
const tabs = {character:'角色档案', dialogue:'对话设定', worldbook:'世界书', images:'图片与头像', more:'更多设置'};
const moreTabs = {astrbot:'AstrBot', history:'版本历史', source:'原始数据'};
const iconPaths = {
  plus:'M12 5v14M5 12h14', upload:'M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5',
  search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  refresh:'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2M18 18A8 8 0 0 1 5 16',
  archive:'M3 3h18v5H3zM5 8v13h14V8M10 12h4', 'arrow-left':'M19 12H5m7-7-7 7 7 7',
  'arrow-up-right':'M7 17 17 7M7 7h10v10', check:'m5 12 4 4L19 6',
  link:'M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2',
  alert:'M12 8v5m0 4h.01M2 21h20L12 3z',
  user:'M20 21v-2a6 6 0 0 0-6-6h-4a6 6 0 0 0-6 6v2M16 5a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  message:'M21 3H3v14h4v4l5-4h9zM7 8h10M7 12h6',
  book:'M12 5v16M12 5C9 2 5 2 2 3v16c4-1 7 0 10 2 3-2 6-3 10-2V3c-3-1-7-1-10 2',
  image:'M3 3h18v18H3zM3 17l6-6 4 4 3-3 5 5M8 7h.01',
  settings:'M4 7h16M4 17h16M8 4v6M16 14v6',
  panel:'M3 3h18v18H3zM15 3v18', x:'m6 6 12 12M6 18 18 6',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${iconPaths[name]||iconPaths.user}"></path></svg>`;
const tabIcons = {character:'user', dialogue:'message', worldbook:'book', images:'image', more:'settings'};
const activeTab = () => Object.hasOwn(moreTabs,state.tab)?'more':state.tab;
function syncBadge(status) {
  const warning=['conflict','failed','missing','pending'].includes(status);
  return `<span class="sync-status ${status==='synced'?'is-synced':warning?'is-warning':''}">${icon(status==='synced'?'check':warning?'alert':'link')}${escape(statusNames[status]||status)}</span>`;
}
const statusNames = {synced:'已同步', unbound:'未绑定', pending:'待同步', failed:'同步失败', conflict:'同步冲突', missing:'人格缺失'};
const date = (value) => value ? new Date(value * 1000).toLocaleString() : '—';

let noticeTimer, noticeHideTimer, noticeDeadline=0, noticeRemaining=0;
function dismissNotice() {
  clearTimeout(noticeTimer); clearTimeout(noticeHideTimer); noticeTimer=null;
  const el=$('#notice');
  if(el.hidden)return;
  el.classList.add('is-leaving');
  noticeHideTimer=setTimeout(()=>{el.hidden=true;el.classList.remove('is-leaving');},160);
}
function pauseNotice() {
  if(!noticeTimer)return;
  noticeRemaining=Math.max(0,noticeDeadline-performance.now());
  clearTimeout(noticeTimer);noticeTimer=null;
}
function resumeNotice() {
  const el=$('#notice');
  if(el.hidden||el.classList.contains('is-leaving')||el.matches(':hover,:focus-within'))return;
  clearTimeout(noticeTimer);
  noticeDeadline=performance.now()+noticeRemaining;
  noticeTimer=setTimeout(dismissNotice,noticeRemaining);
}
function notice(message, error=false) {
  clearTimeout(noticeTimer);clearTimeout(noticeHideTimer);noticeTimer=null;
  const el=$('#notice');
  el.classList.remove('is-leaving');
  el.hidden=!message;
  $('#notice-message').textContent=message||'';
  $('#notice-symbol').innerHTML=icon(error?'alert':'check');
  el.dataset.error=String(error);
  el.setAttribute('role',error?'alert':'status');
  el.setAttribute('aria-live',error?'assertive':'polite');
  noticeRemaining=error?8000:3000;
  resumeNotice();
}
function dirty() { return !!state.current && (Object.keys(state.pendingImages).length>0 || state.raw !== null || !!state.tagDraft.trim() || worldbook.hasDraft() || JSON.stringify(state.document)!==JSON.stringify(state.current.document) || JSON.stringify(state.options)!==JSON.stringify(state.current.options)); }
function draftDocument() { try { return state.raw === null ? worldbook.draft(state.document) : JSON.parse(state.raw); } catch { return state.document; } }
function versionChanged() { return !!state.current && draftDocument()?.data?.character_version!==state.current.document.data.character_version; }
function field(label, key, value, {area=false, hint='', scope='data', rows=3, readonly=false}={}) {
  const attr=`data-scope="${scope}" data-field="${escape(key)}" ${readonly?'readonly':''}`;
  return `<label class="field"><span class="field-label">${label}<code>${escape(key)}</code></span>${area?`<textarea ${attr} rows="${rows}">${escape(value)}</textarea>`:`<input ${attr} value="${escape(value)}">`}${hint?`<span class="field-help">${hint}</span>`:''}</label>`;
}
function tagChips() {
  return state.document.data.tags.map((tag,index)=>`<span class="tag-chip"><span>${escape(tag)}</span><button type="button" data-remove-tag="${index}" aria-label="移除标签：${escape(tag)}">${icon('x')}</button></span>`).join('');
}
function tagField() {
  return `<div class="field"><label class="field-label" id="tags-label" for="tag-input">标签<code>tags</code></label><div class="tag-editor" role="group" aria-labelledby="tags-label" aria-describedby="tags-help"><div class="tag-chip-list">${tagChips()}</div><input id="tag-input" type="text" value="${escape(state.tagDraft)}" placeholder="添加标签…" aria-label="添加标签" aria-describedby="tags-help" autocomplete="off" enterkeyhint="done"><button type="button" class="tag-add" data-action="add-tag" aria-label="添加输入的标签">${icon('plus')}添加</button></div><span class="field-help" id="tags-help">输入后按 Enter 添加，也可粘贴多个标签。</span><span class="field-help tag-feedback" id="tag-feedback" role="status"></span></div>`;
}
function tagsEditable() { return state.current&&!state.current.locked&&!state.current.deleted&&!state.current.sync.core_locked; }
function updateTagControl() {
  const input=$('#tag-input'), add=$('[data-action="add-tag"]');
  if(!input||!add)return;
  add.disabled=state.busy||!tagsEditable()||!input.value.trim();
}
function renderTagChips(message='') {
  const list=$('.tag-chip-list');
  if(list)list.innerHTML=tagChips();
  if($('#tag-feedback'))$('#tag-feedback').textContent=message;
  const caption=$('.identity-caption');
  if(caption)caption.textContent=state.document.data.tags.join(' · ')||'为这个角色，写下独一无二的故事。';
  applyDisabled(); updateSavebar();
}
function commitTagDraft() {
  if(!tagsEditable())return;
  const values=state.tagDraft.split(/[、,，\r\n]+/).map(tag=>tag.trim()).filter(Boolean);
  const added=[], existing=[];
  for(const tag of values){
    if(state.document.data.tags.includes(tag))existing.push(tag);
    else{state.document.data.tags.push(tag);added.push(tag);}
  }
  state.tagDraft='';
  if($('#tag-input'))$('#tag-input').value='';
  renderTagChips(added.length?`已添加 ${added.length} 个标签。`:existing.length?'这些标签已经存在。':'');
}
function removeTag(index) {
  if(!tagsEditable()||state.busy)return;
  const [removed]=state.document.data.tags.splice(index,1);
  renderTagChips(`已移除标签「${removed}」。`);
  $('#tag-input')?.focus({preventScroll:true});
}
function modal(title, body) { $('#dialog-title').textContent=title; $('#dialog-body').innerHTML=body; $('#dialog').showModal(); }
function closeModal() { $('#dialog').close(); }
function updateDeleteControl() {
  const button=$('[data-action="delete-confirm"]');
  if(button){
    button.disabled=state.busy||!state.current?.deleted||state.current.locked||state.current.sync.core_locked||$('#delete-name')?.value!==state.current.document.data.name;
    button.textContent=$('#delete-persona')?.checked?'删除角色卡和人格':'永久删除角色卡';
  }
  const help=$('#delete-persona-help');
  if(help&&!$('#delete-persona').disabled)help.textContent=$('#delete-persona').checked?'将同时删除此人格。若仍被配置、会话、子代理或任务使用，本次删除会被阻止，角色卡与人格均保留。人格备份和聊天记录会保留。':'默认仅删除角色卡，绑定的人格、人格备份和现有对话会保留。';
}
function openDeleteDialog() {
  const c=state.current, name=c.document.data.name;
  const canDeletePersona=c.persona_id&&c.persona_id!=='default'&&c.sync.current&&c.sync.same_record;
  const personaHelp=c.persona_id==='default'?'系统默认人格不能删除，本次仅删除角色卡。':'原绑定人格已不存在或已被同名记录替换，本次仅删除角色卡。';
  const personaOption=c.persona_id?`<div class="delete-persona-option"><label class="delete-persona-label" for="delete-persona"><input id="delete-persona" type="checkbox" aria-describedby="delete-persona-help" ${canDeletePersona?'':'disabled'}><span><strong>同时删除绑定的 AstrBot 人格</strong><span class="small muted delete-persona-id">${escape(c.persona_id)}</span></span></label><p id="delete-persona-help" class="small muted">${canDeletePersona?'':personaHelp}</p></div>`:'';
  modal(`永久删除「${name}」`, `<div class="form"><p>将永久删除这张角色卡及其 <strong>${c.snapshots.length} 个历史快照</strong>，并清理不再使用的图片资源。删除后无法恢复。</p>${personaOption}<p class="small muted">${c.persona_id?'':'此卡片未绑定 AstrBot 人格，现有对话不受影响。'}你可以先导出当前角色卡留作备份。</p><label class="field" for="delete-name"><span class="field-label">输入角色名称以确认：<strong>${escape(name)}</strong></span><input id="delete-name" autocomplete="off" spellcheck="false" aria-describedby="delete-error"></label><p id="delete-error" class="small error" role="alert" hidden></p></div><div class="dialog-footer"><button data-action="export">导出角色卡</button><button data-action="cancel-delete">取消</button><button data-action="delete-confirm" class="destructive" disabled>永久删除角色卡</button></div>`);
  updateDeleteControl();
  $('#delete-name').focus();
}
async function confirmAction(message) {
  $('#confirm-text').textContent=message;
  document.querySelectorAll('[data-confirm]').forEach(el=>el.disabled=false);
  const dialog=$('#confirm-dialog');dialog.returnValue='';dialog.showModal();
  return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true}));
}
async function requireClean() { return !dirty() || await confirmAction('当前编辑尚未保存，放弃这些修改并继续？'); }
async function busy(work) {
  if(state.busy) return;
  state.busy=true;
  document.querySelectorAll('button,input,textarea,select').forEach(b=>{b.dataset.wasDisabled=String(b.disabled);b.disabled=true;});
  try { return await work(); }
  catch(error) { notice(error.message || String(error), true); }
  finally { state.busy=false; document.querySelectorAll('button,input,textarea,select').forEach(b=>{b.disabled=b.dataset.wasDisabled==='true';delete b.dataset.wasDisabled;}); applyDisabled(); updateSavebar(); }
}
const get=(path,params={})=>bridge.apiGet(path,params);
const post=(action,payload)=>bridge.apiPost('action',{action,payload});
const worldbook=new WorldbookEditor(state,{escape,icon,post,changed:()=>{renderPreview();updateSavebar();},applyDisabled});
const galleryImages = new Map();
let imageObserver;
function imageKey(card) { return `${card.card_id}:${card.revision}`; }
function applyGalleryImage(cover,result) {
  if(!cover.isConnected || !validImageUrl(result?.data_url))return;
  const image=new Image(); image.alt=''; image.decoding='async';
  image.addEventListener('load',()=>cover.classList.add('has-image'),{once:true});
  cover.append(image); image.src=result.data_url;
}
async function loadGalleryImage(cover) {
  const card=state.items.find(item=>item.card_id===cover.dataset.cover);
  if(!card)return;
  const key=imageKey(card);
  if(!galleryImages.has(key))galleryImages.set(key,(async()=>{
    if(card.asset_id){
      try { const result=await get('card-image',{card_id:card.card_id,thumbnail:true});if(result.revision===card.revision&&validImageUrl(result.data_url))return result; }catch { /* Keep the placeholder if the cover cannot be loaded. */ }
    }
    const result=await get('avatar',{card_id:card.card_id,thumbnail:true});
    return result.revision===card.revision?result:null;
  })().catch(()=>null));
  applyGalleryImage(cover,await galleryImages.get(key));
}
function observeGallery() {
  imageObserver?.disconnect();
  if(state.view!=='collection')return;
  const covers=$('#card-list').querySelectorAll('[data-cover]');
  if(!('IntersectionObserver' in window)){covers.forEach(cover=>void loadGalleryImage(cover));return;}
  imageObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{
    if(entry.isIntersecting){imageObserver.unobserve(entry.target);void loadGalleryImage(entry.target);}
  }),{rootMargin:'150px'});
  covers.forEach(cover=>imageObserver.observe(cover));
}
function collectionScope() {
  document.querySelectorAll('[data-collection]').forEach(button=>button.setAttribute('aria-pressed',String((button.dataset.collection==='archived')===state.deleted)));
  $('#collection-title').textContent=state.deleted?'旧卷暂藏，来日续章。':'一笔一世界，一卷一浮生。';
}
async function showCollection() {
  clearTimeout(previewTimer); ++previewRevision;
  state.current=null; state.document=null; state.options=null; state.raw=null; state.tagDraft=''; state.pendingImages={}; state.view='collection';
  $('#editor').replaceChildren(); $('#workspace').hidden=true; $('#collection').hidden=false;
  await loadList(); render();
  window.scrollTo({top:0});
  $('#search').focus({preventScroll:true});
}

async function loadList() {
  const items=[];
  let offset=0;
  while(true) {
    const page=await get('cards',{offset,limit:100,deleted:String(state.deleted)});
    if(offset===0){const errors=page.conversion_errors||[];const el=$('#conversion-notice');el.hidden=!errors.length;el.textContent=errors.map(e=>`「${e.persona_id}」未能转为角色卡：${e.message}`).join('；');}
    items.push(...page.items); offset+=page.items.length;
    if(offset>=page.total || !page.items.length) break;
  }
  state.items=items;
  if(!state.deleted)state.activeCount=items.length;
  const keys=new Set(items.map(imageKey));
  for(const key of galleryImages.keys())if(!keys.has(key))galleryImages.delete(key);
  renderLibrary();
}
function renderLibrary() {
  const query=$('#search').value.trim().toLowerCase();
  const visible=state.items.filter(c=>[c.name,c.description,...(c.tags||[])].join(' ').toLowerCase().includes(query));
  $('#card-count').textContent=String(state.activeCount);
  collectionScope();
  $('#card-list').innerHTML=visible.map(c=>`
    <button class="card-item" data-card="${escape(c.card_id)}" aria-label="编辑角色：${escape(c.name)}">
      <span class="card-cover" data-cover="${escape(c.card_id)}"><span class="cover-letter" aria-hidden="true">${escape(Array.from(c.name)[0]||'✦')}</span><span class="cover-version">人物小传</span><span class="cover-hint">故事待续</span></span>
      <span class="card-body">
        <span class="card-title-row"><span class="item-name">${escape(c.name)}</span><span class="card-version" title="角色版本：${escape(c.character_version||'未定版')}">${escape(c.character_version||'未定版')}</span></span>
        <span class="card-description">${escape(c.description||(c.persona_import?'从 AstrBot 转入的角色，继续完善它的故事。':'故事还未写下，等待你来补全。'))}</span>
        <span class="card-tags">${(c.tags?.length?c.tags.slice(0,2):[c.persona_import?'来自 AstrBot':'原创角色']).map(tag=>`<span class="tag">${escape(tag)}</span>`).join('')}</span>
        ${c.locked||c.deleted?`<span class="card-flags">${c.locked?'已锁定':''}${c.deleted?' 已归档':''}</span>`:''}
        <span class="card-footer">${syncBadge(c.sync_status)}<span class="card-edit">编辑角色 ${icon('arrow-up-right')}</span></span>
      </span>
    </button>`).join('') || `<div class="empty-state">${icon(query?'search':state.deleted?'archive':'book')}<h3>${query?'没有找到这个角色':state.deleted?'暂无已归档角色':'从一个角色，开始新的故事'}</h3><p>${query?'换个名称、简介或标签试试。':state.deleted?'归档的角色会保留在这里。':'新建角色，或导入已有的 JSON / PNG 角色卡。'}</p>${!query&&!state.deleted?'<div class="actions"><button data-action="new-empty" class="primary">新建角色</button><button data-action="import-empty">导入角色卡</button></div>':''}</div>`;
  const choices=state.current&&!state.items.some(c=>c.card_id===state.current.card_id)?[{card_id:state.current.card_id,name:state.current.document.data.name},...state.items]:state.items;
  $('#card-switcher').innerHTML=choices.map(c=>`<option value="${escape(c.card_id)}" ${c.card_id===state.current?.card_id?'selected':''}>${escape(c.name)}</option>`).join('');
  observeGallery();
}
async function openCard(id) {
  const card=await get('card',{card_id:id});
  state.tab='character'; adopt(card); render();
  window.scrollTo({top:0});
  $('#identity h2').focus({preventScroll:true});
}
async function loadAvatar(card) {
  try {
    const result=await get('avatar',{card_id:card.card_id});
    if(state.current!==card || result.revision!==card.revision)return;
    state.avatar=/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$/.test(result.data_url||'')?result.data_url:null;
    state.avatarWarning=result.warning||null;
  } catch(error) {
    if(state.current!==card)return;
    state.avatarWarning=error.message||'主头像暂时无法读取，请刷新重试。';
  }
  renderIdentity();
  if(state.tab==='images')renderImagePanel();
}
async function loadCardImage(card) {
  if(!card.asset_id)return;
  try {
    const result=await get('card-image',{card_id:card.card_id});
    if(state.current!==card || result.revision!==card.revision)return;
    state.cardImage=validImageUrl(result.data_url)?result.data_url:null;
    state.cardImageWarning=result.warning||null;
  } catch(error) {
    if(state.current!==card)return;
    state.cardImageWarning=error.message||'角色卡图片暂时无法读取，请刷新重试。';
  }
  renderIdentity();
  if(state.tab==='images')renderImagePanel();
}
function validImageUrl(url) { return /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$/.test(url||''); }
function followsCardImage() {
  if(state.pendingImages.avatar)return state.pendingImages.avatar.upload_id==='ccdefault';
  const assets=draftDocument()?.data?.assets;
  if(assets===undefined)return true;
  if(!Array.isArray(assets))return false;
  const icons=assets.filter(a=>a?.type==='icon');
  const main=icons.find(a=>a.name==='main')||(icons.length===1?icons[0]:null);
  return typeof main?.uri==='string' && main.uri.toLowerCase()==='ccdefault:';
}
function cardImageUrl() { return state.pendingImages.card_image?.data_url||state.cardImage; }
function avatarUrl() {
  const selected=state.pendingImages.avatar;
  if(selected && selected.upload_id!=='ccdefault')return selected.data_url;
  if(followsCardImage() && (selected || state.pendingImages.card_image))return cardImageUrl();
  return state.avatar;
}
function avatarWarning() { return state.pendingImages.avatar || (state.pendingImages.card_image && followsCardImage())?null:state.avatarWarning; }
function adopt(card) {
  worldbook.reset(state.current?.card_id===card.card_id);
  state.current=card; state.document=clone(card.document); state.options=clone(card.options); state.raw=null; state.tagDraft=''; state.avatar=null; state.avatarWarning=null; state.cardImage=null; state.cardImageWarning=null; state.pendingImages={}; state.view='editor';
  void loadAvatar(card);
  void loadCardImage(card);
  const index=state.items.findIndex(c=>c.card_id===card.card_id);
  if(index>=0) Object.assign(state.items[index],{name:card.document.data.name,description:card.document.data.description.slice(0,240),tags:card.document.data.tags,asset_id:card.asset_id,character_version:card.document.data.character_version,spec:card.document.spec,revision:card.revision,locked:card.locked,deleted:card.deleted,sync_status:card.sync.status,persona_import:card.compiled.persona_import});
}
function renderIdentity() {
  if(!state.current || !$('#identity')) return;
  const focused=$('#identity h2')===document.activeElement;
  const d=state.document.data, image=avatarUrl(), warning=avatarWarning();
  $('#identity').innerHTML=`<div class="avatar">${image?`<img src="${image}" alt="${escape(d.name)}的角色图">`:escape(Array.from(d.name)[0]||'✦')}</div><div class="identity-main"><h2 tabindex="-1">${escape(d.name)}</h2><p class="identity-caption">${escape(d.tags.join(' · ')||'为这个角色，写下独一无二的故事。')}</p><div class="small"><span class="tag">V3</span>${state.current.compiled.persona_import?'<span class="tag">来自 AstrBot</span>':''}<span class="muted">版本 ${escape(d.character_version||'未设置')}</span>${syncBadge(state.current.sync.status)}${state.current.locked?'<span class="warning">已锁定</span>':state.current.sync.core_locked?'<span class="warning">人格已锁定</span>':''}${state.current.deleted?'<span class="warning">已归档</span>':''}<span id="dirty-label" class="warning"></span></div>${warning?`<p class="small warning">主头像：${escape(warning)}</p>`:''}</div><button data-action="export">${icon('upload')}导出</button>`;
  $('#dirty-label').textContent=dirty()?'未保存':'';
  if(focused)$('#identity h2').focus({preventScroll:true});
}
function render() {
  $('#collection').hidden=state.view!=='collection'; $('#workspace').hidden=state.view!=='editor';
  const actions=$('#studio-actions'), actionHost=$(state.view==='editor'?'#editor-topline':'#collection-heading');
  if(actions.parentElement!==actionHost)actionHost.append(actions);
  renderLibrary();
  $('#studio').classList.toggle('show-keys',state.showKeys);
  if(!state.current){$('#editor').replaceChildren();return;}
  const selected=activeTab();
  $('#editor').innerHTML=`<div class="identity" id="identity"></div>${state.current.deleted?`<section class="archive-banner" aria-label="归档角色操作"><div><h3>这个角色已归档</h3><p class="small muted">恢复后可继续编辑当前内容，历史快照也会保留。</p>${state.current.locked?'<p class="small warning">角色卡已锁定，请先在更多设置中人工解锁。</p>':state.current.sync.core_locked?'<p class="small warning">绑定的 AstrBot 人格已锁定，请先人工解锁后刷新。</p>':''}</div><div class="actions"><button data-action="unarchive" class="primary">恢复到角色库</button><button data-action="delete" class="danger">永久删除</button></div></section>`:''}<div class="tabs" role="tablist" aria-label="角色编辑分区">${Object.entries(tabs).map(([key,label])=>`<button role="tab" tabindex="${selected===key?0:-1}" aria-selected="${selected===key}" aria-controls="panel" id="tab-${key}" data-tab="${key}">${icon(tabIcons[key])}${label}</button>`).join('')}</div><div class="editor-columns ${state.previewOpen?'preview-open':''}" id="editor-columns"><div id="panel" role="tabpanel" aria-labelledby="tab-${selected}"></div><aside id="preview" class="preview" aria-label="运行预览" ${state.previewOpen?'':'hidden'}></aside></div><div class="savebar"><div role="status"><div id="save-state"></div><p id="save-help" class="small muted"></p></div><div class="save-actions"><button class="quiet" data-action="discard" id="discard">放弃修改</button><button class="quiet" data-action="preview" aria-expanded="${state.previewOpen}" aria-controls="preview">${icon('panel')}${state.previewOpen?'收起预览':'运行预览'}</button><button class="primary" data-action="save" id="save">保存并同步</button></div></div>`;
  renderIdentity(); renderPanel(); renderPreview(); updateSavebar();
}
function applyDisabled() {
  if(!state.current)return;
  const blocked=state.current.locked || state.current.deleted || state.current.sync.core_locked;
  if(blocked)document.querySelectorAll('#panel input:not([data-wb-test]),#panel textarea:not([data-wb-test]),#panel select').forEach(el=>el.disabled=true);
  document.querySelectorAll('[data-wb-action]:not([data-wb-action="test"])').forEach(el=>el.disabled=state.busy||blocked||(el.dataset.wbAction==='add'&&worldbook.entries().length>=500));
  document.querySelectorAll('[data-wb-select],[data-wb-action="test"]').forEach(el=>el.disabled=state.busy);
  document.querySelectorAll('[data-add-greeting],[data-remove-greeting],[data-remove-tag],[data-action=archive],[data-action=bind],[data-action=unbind],[data-action=sync],[data-action=overwrite],[data-image-upload],[data-image-discard],[data-action=avatar-follow]').forEach(el=>el.disabled=state.busy || blocked);
  document.querySelectorAll('[data-action=restore],[data-action=unarchive],[data-action=delete]').forEach(el=>el.disabled=state.busy || !!state.current.locked || !!state.current.sync.core_locked);
  document.querySelectorAll('[data-action=lock]').forEach(el=>el.disabled=state.busy || (!!state.current.deleted&&!state.current.locked));
  updateTagControl();
  updateDeleteControl();
}
function renderPanel() {
  const d=state.document.data, c=state.current;
  const hint='<label class="key-toggle"><input type="checkbox" id="show-keys" '+(state.showKeys?'checked':'')+'>显示原始字段名</label>';
  if(state.tab==='character') $('#panel').innerHTML=`<div class="panel-heading"><h3>角色档案</h3>${hint}</div><div class="form"><div class="two-columns identity-fields">${field('角色名称','name',d.name)}${field('角色版本','character_version',d.character_version,{hint:'修改版本并保存时留存快照'})}</div>${field('角色描述','description',d.description,{area:true,rows:5})}${field('性格与表达方式','personality',d.personality,{area:true})}${field('场景','scenario',d.scenario,{area:true})}<details class="metadata-fields"><summary>作者、标签与备注</summary><div class="form"><div class="two-columns">${field('作者','creator',d.creator)}${tagField()}</div>${field('创作者备注','creator_notes',d.creator_notes,{area:true,hint:'用于管理和导出，默认不进入模型上下文'})}</div></details></div>`;
  if(state.tab==='dialogue') $('#panel').innerHTML=`<div class="form">${field('角色指令','system_prompt',d.system_prompt,{area:true,hint:c.compiled.persona_import?'从 AstrBot 转入的原提示词原样保留；编辑后支持角色卡文本宏':'为空时使用默认角色指令；支持 {{char}}、{{user}} 和 {{original}}'})}${field('历史后指令','post_history_instructions',d.post_history_instructions,{area:true,hint:'作为本轮临时补充内容附在用户消息后，不修改已存历史'})}${field('示例对话','mes_example',d.mes_example,{area:true,rows:6,hint:c.compiled.persona_import?'原预设对话未编辑时按 AstrBot 原方式运行；编辑后使用角色卡示例格式':'支持 <START>、{{user}}: 和 {{char}}:；无法解析的格式保留为提示词原文'})}${field('开场白','first_mes',d.first_mes,{area:true,hint:'新对话的角色背景消息，不会重复写入聊天历史'})}${greetings('alternate_greetings','备用开场白')}${greetings('group_only_greetings','群聊开场白')}<label class="field"><span class="field-label">默认开场白</span><select data-scope="options-number" data-field="greeting_index">${[d.first_mes,...d.alternate_greetings].map((v,i)=>`<option value="${i}" ${state.options.greeting_index===i?'selected':''}>${i===0?'主开场白':`备用 ${i}`} · ${escape(v.slice(0,25)||'空白')}</option>`).join('')}</select><span class="field-help">有群聊专用开场白时使用群聊列表中的同一序号；超出序号时不注入。</span></label></div>`;
  if(state.tab==='images') renderImagePanel();
  if(state.tab==='worldbook')worldbook.render();
  if(state.tab==='astrbot') renderAstrbot();
  if(state.tab==='history') $('#panel').innerHTML=`<p class="small muted">初始保存和角色版本变更时留档，长期保留。恢复不会产生额外快照。</p>${c.snapshots.map(s=>`<article class="snapshot"><div class="row"><h3>版本 ${escape(s.character_version||'未设置')}</h3><span class="small muted">${date(s.created_at)}</span></div><p>${({create:'初始创建',import:'初始导入',builtin:'内置角色初始导入',persona:'从 AstrBot 人格转换',version:'角色版本变更'})[s.reason]||escape(s.reason)}</p><div class="actions"><button data-snapshot="${escape(s.snapshot_id)}">查看与比较</button></div></article>`).join('')}`;
  if(state.tab==='source') $('#panel').innerHTML=`<div class="form"><div class="notice">世界书按已支持的触发规则参与对话；未知字段和扩展原样保存，不执行酒馆脚本或正则。</div><label class="field"><span class="field-label">完整角色卡 JSON</span><textarea class="raw-editor" id="raw-json" spellcheck="false">${escape(state.raw??JSON.stringify(state.document,null,2))}</textarea><span class="field-help">此处编辑完整文档；保存时校验并统一为 V3。它不会修改绑定或权限信息。assets 中的 icon/main 可指定主头像；“图片与头像”中待保存的选择会在保存时应用。</span></label><div class="details-row"><span>卡片创建</span><span>${date(c.created_at)}</span></div><div class="details-row"><span>卡片最近保存</span><span>${date(c.updated_at)}</span></div>${d.creation_date?`<div class="details-row"><span>作者创建日期</span><span>${date(d.creation_date)}</span></div>`:''}${d.modification_date?`<div class="details-row"><span>作者修改日期</span><span>${date(d.modification_date)}</span></div>`:''}</div>`;
  if(Object.hasOwn(moreTabs,state.tab))$('#panel').insertAdjacentHTML('afterbegin',`<div class="subtabs" role="group" aria-label="更多设置分区">${Object.entries(moreTabs).map(([key,label])=>`<button data-tab="${key}" aria-pressed="${state.tab===key}">${label}</button>`).join('')}</div>`);
  applyDisabled();
}
function renderImagePanel() {
  if(!state.current || state.tab!=='images' || !$('#panel'))return;
  const photo=cardImageUrl(), avatar=avatarUrl();
  const imageCard=(kind,title,url,description,warning)=>`<section class="image-card"><div class="row"><h3>${title}</h3>${state.pendingImages[kind]?'<span class="tag warning">待保存</span>':''}</div><div class="image-preview ${kind==='avatar'?'image-avatar':''}">${url?`<img src="${url}" alt="${title}预览">`:'<span class="muted small">尚未设置</span>'}</div><p class="small muted">${description}</p>${warning?`<p class="small warning">${escape(warning)}</p>`:''}<div class="actions"><button data-image-upload="${kind}">${url?'替换':'添加'}${title}</button>${state.pendingImages[kind]?`<button class="quiet" data-image-discard="${kind}">撤销本次选择</button>`:''}</div>${kind==='avatar'?'<button class="quiet" data-action="avatar-follow">使用角色卡图片</button>':''}</section>`;
  $('#panel').innerHTML=`<p class="small muted image-intro">支持 PNG、JPEG、WebP，文件最大 20 MiB。选择后可预览，点击“保存并同步”生效。</p><div class="image-grid">${imageCard('card_image','角色卡图片',photo,'用于 PNG 导出的底图；未设置时使用头像。',state.pendingImages.card_image?null:state.cardImageWarning)}${imageCard('avatar','头像',avatar,'用于角色头像，可单独上传；最长边自动缩放至 512 像素。',avatarWarning())}</div><p class="small muted section">图片替换按普通编辑保存；同时修改角色版本时，图片会随完整快照留档。</p>`;
  applyDisabled();
}
function greetings(key,label) {
  const values=state.document.data[key]||[];
  return `<section><div class="row"><h3>${label}</h3><button data-add-greeting="${key}">添加</button></div>${values.map((v,i)=>`<div class="greeting-entry"><label class="field"><span class="field-label">${label} ${i+1}</span><textarea data-greeting="${key}" data-index="${i}" rows="3">${escape(v)}</textarea></label><button class="quiet" data-remove-greeting="${key}" data-index="${i}">移除此条</button></div>`).join('')}</section>`;
}
function capability(key,label) {
  const value=state.options[key]; const mode=value===null?'all':value.length?'list':'none';
  return `<label class="field"><span class="field-label">${label}</span><select data-mode="${key}"><option value="all" ${mode==='all'?'selected':''}>全部可用</option><option value="none" ${mode==='none'?'selected':''}>全部禁用</option><option value="list" ${mode==='list'?'selected':''}>指定名称</option></select></label>${mode==='list'?field('名称列表',key,value.join('\n'),{scope:'capability-list',area:true,hint:'每行一个已注册的名称'}):''}`;
}
function renderAstrbot() {
  const c=state.current, sync=c.sync;
  const lockHint=sync.core_locked?`<div class="notice">绑定的人格已被人工锁定，请在聊天中发送 /fs unlock ${escape(c.persona_id)} 后刷新。</div>`:'';
  const abnormal=['conflict','failed','missing','pending'].includes(sync.status);
  $('#panel').innerHTML=`${lockHint}${abnormal?`<div class="notice"><p>${escape(sync.error||statusNames[sync.status])}</p><div class="actions">${sync.status==='conflict'&&sync.same_record?'<button data-action="sync-diff">查看差异</button>':''}${['failed','pending'].includes(sync.status)?'<button data-action="sync">手动重试同步</button>':''}</div></div>`:''}<div class="details-row"><span>绑定人格</span><span>${escape(c.persona_id||'尚未绑定')}</span></div><div class="details-row"><span>同步状态</span><span class="${sync.status==='synced'?'ok':'warning'}">${statusNames[sync.status]||escape(sync.status)}</span></div><div class="details-row"><span>创建者</span><span>${escape(c.owner?c.owner.join(' / '):'未设归属 · 由管理员管理')}</span></div><div class="actions section">${c.persona_id?'<button data-action="unbind">解除绑定</button>':'<button data-action="bind">创建并绑定人格</button>'}<button data-action="lock">${c.locked?'解锁角色卡':'锁定角色卡'}</button>${!c.deleted?'<button data-action="archive" class="danger">归档角色卡</button>':''}</div><div class="form section">${capability('tools','可用工具')}${capability('skills','可用技能')}${field('自定义错误回复','custom_error_message',state.options.custom_error_message||'',{scope:'options-null',area:true,hint:'留空时清除。此项及工具、技能独立于人物卡标准字段。'})}</div>`;
}
let previewTimer, previewRevision=0;
function renderPreview() {
  clearTimeout(previewTimer);
  const revision=++previewRevision;
  if(!state.current||!state.previewOpen||!$('#preview'))return;
  const card=clone(draftDocument()), options=clone(state.options);
  $('#preview').hidden=false;
  if(!$('#preview').textContent)$('#preview').innerHTML='<p class="small muted" role="status">正在生成运行预览…</p>';
  previewTimer=setTimeout(async()=>{
    try {
      const result=await post('preview',{card,options});
      if(revision!==previewRevision || !state.current || !state.previewOpen)return;
      const d=result.document.data, compiled=result.compiled;
      const replace=text=>String(text||'').replace(/\{\{(char|user)\}\}/g,(_,key)=>key==='char'?d.name:'访客');
      const original=compiled.literal_system_prompt?d.extensions?.astrbot_persona_studio?.persona_import?.persona?.system_prompt||'':'';
      const prompt=original+replace(compiled.system_prompt.slice(original.length));
      const messages=[...(compiled.begin_dialogs||[]).map((content,i)=>({role:i%2?'assistant':'user',content})),...compiled.examples.flat().map(m=>({...m,content:replace(m.content)}))];
      const greetings=[d.first_mes,...d.alternate_greetings];
      const book=d.character_book?.entries?.length||0;
      $('#preview').innerHTML=`<div class="preview-head"><h2>运行预览</h2><button class="quiet icon-button" data-action="preview" aria-label="收起运行预览">${icon('x')}</button></div><p class="small muted">实际编译结果 · 用户名示例：访客</p><div class="preview-columns"><section class="section"><div class="section-label">选定开场白（私聊）</div><div class="greeting">${escape(replace(greetings[options.greeting_index])||'未设置')}</div></section><section class="section"><div class="section-label">人格提示词</div><pre>${escape(prompt)}</pre></section><section class="section"><div class="section-label">示例与预设对话</div><pre>${escape(messages.map(m=>`${m.role}: ${m.content}`).join('\n')||'未设置')}</pre></section>${d.post_history_instructions?`<section class="section"><div class="section-label">历史后临时补充</div><pre>${escape(replace(d.post_history_instructions))}</pre></section>`:''}<section class="section"><div class="section-label">世界书</div><p class="small">${book?`${book} 条设定 · 按每轮对话触发`:'未附带世界书'}</p><p class="small muted">在“世界书”页签输入测试消息，查看实际注入内容。</p></section>${compiled.warnings.length?`<section class="section"><div class="section-label">适配说明</div>${compiled.warnings.map(w=>`<p class="small muted">${escape(w)}</p>`).join('')}</section>`:''}</div>`;
    }catch(error){if(revision===previewRevision)$('#preview').innerHTML=`<h2>运行预览</h2><p class="warning">${escape(error.message)}</p>`;}
  },250);
}
function updateSavebar() {
  if(!state.current || !$('#save'))return;
  const changed=dirty(), version=versionChanged();
  $('#discard').hidden=!changed;
  $('#save').disabled=state.busy||!changed||state.current.locked||state.current.deleted||state.current.sync.core_locked;
  $('#save').hidden=!!state.current.deleted;
  $('#save').textContent=state.busy?'正在保存…':version?'保存、定版并同步':'保存并同步';
  $('#save-state').textContent=changed?'编辑尚未保存':`卡片已保存 · ${statusNames[state.current.sync.status]||state.current.sync.status}`;
  $('#save-state').className=!changed&&state.current.sync.status==='synced'?'ok small':'small';
  $('#save-help').textContent=version?`角色版本 ${state.current.document.data.character_version||'未设置'} → ${draftDocument()?.data?.character_version||'未设置'}，将新增快照`:'普通保存不生成快照';
  if($('#dirty-label'))$('#dirty-label').textContent=changed?'未保存':'';
  if(state.current.deleted){$('#save-state').textContent='角色已归档';$('#save-state').className='small';$('#save-help').textContent='内容与快照长期保留，可恢复到角色库或永久删除。';}
}
async function save() {
  commitTagDraft();
  worldbook.flush();
  worldbook.validate();
  if(state.raw!==null) {
    const parsed=JSON.parse(state.raw);
    if(!parsed.data || typeof parsed.data.name!=='string')throw Error('角色卡必须包含 data.name。');
    state.document=parsed;
  }
  const images=Object.fromEntries(Object.entries(state.pendingImages).map(([kind,value])=>[kind,value.upload_id]));
  const card=await post('update',{card_id:state.current.card_id,expected_revision:state.current.revision,card:state.document,options:state.options,...(Object.keys(images).length?{images}:{})});
  const snapshotAdded=card.snapshots.length>state.current.snapshots.length;
  adopt(card);render();
  notice(card.sync.status==='synced'?`卡片已保存并同步${snapshotAdded?'，已保存角色版本快照':'，未新增快照'}。`:`卡片已保存，${statusNames[card.sync.status]}。${card.sync.error||''}`,['failed','conflict','missing'].includes(card.sync.status));
}
async function mutation(action,extra={}) {
  if(dirty())throw Error('请先保存或放弃当前编辑，再执行此操作。');
  const card=await post(action,{card_id:state.current.card_id,expected_revision:state.current.revision,...extra});
  if(action==='unarchive')state.deleted=false;
  adopt(card);await loadList();render();
  notice(`操作已完成。${card.sync.error||''}`,['failed','conflict','missing'].includes(card.sync.status));
}
function diffHtml(before,after) {
  const keys=[...new Set([...Object.keys(before),...Object.keys(after)])];
  const changes=keys.filter(key=>JSON.stringify(before[key])!==JSON.stringify(after[key]));
  const format=v=>typeof v==='string'?v:JSON.stringify(v,null,2)??'（不存在）';
  return changes.map(key=>`<section class="diff-section"><h3>${escape(key)}</h3><div class="before"><p class="small muted">当前</p><pre>${escape(format(before[key]))}</pre></div><div class="after"><p class="small muted">目标</p><pre>${escape(format(after[key]))}</pre></div></section>`).join('')||'<p class="muted">内容相同</p>';
}

$('#dialog-close').addEventListener('click',closeModal);
$('#notice-close').addEventListener('click',dismissNotice);
$('#notice').addEventListener('pointerenter',event=>{if(event.pointerType==='mouse')pauseNotice();});
$('#notice').addEventListener('pointerleave',event=>{if(event.pointerType==='mouse')resumeNotice();});
$('#notice').addEventListener('focusin',pauseNotice);
$('#notice').addEventListener('focusout',event=>{if(!event.currentTarget.contains(event.relatedTarget))resumeNotice();});
$('#search').addEventListener('input',renderLibrary);
$('#refresh-button').addEventListener('click',()=>busy(async()=>{if(!await requireClean())return;galleryImages.clear();await loadList();if(state.current)await openCard(state.current.card_id);notice('已刷新。');}));
document.querySelectorAll('[data-collection]').forEach(button=>button.addEventListener('click',()=>busy(async()=>{
  if(!await requireClean())return;
  state.deleted=button.dataset.collection==='archived'; await showCollection();
})));
$('#card-switcher').addEventListener('change',event=>{
  const id=event.target.value;
  busy(async()=>{if(await requireClean())await openCard(id);else $('#card-switcher').value=state.current.card_id;});
});
$('#new-button').addEventListener('click',async()=>{
  if(state.busy || !await requireClean())return;
  modal('新建角色卡',`<div class="form"><label class="field">角色名称<input id="new-name" value="新角色" maxlength="255"></label><p class="small muted">使用 Character Card V3 格式。</p><label class="checkbox-label"><input id="new-bind" type="checkbox" checked>同时创建并绑定 AstrBot 人格</label></div><div class="dialog-footer"><button data-action="create-confirm" class="primary">创建角色卡</button></div>`);
});
$('#import-button').addEventListener('click',()=>{if(!state.busy)$('#upload').click();});
$('#upload').addEventListener('change',()=>busy(async()=>{
  const file=$('#upload').files[0];$('#upload').value='';if(!file)return;
  if(file.size>20*1024*1024)throw Error('文件最大为 20 MiB。');
  const result=await bridge.upload('import',file);
  const d=result.card.data;
  const avatarHint=({ccdefault:result.has_image?'使用原始 PNG':'无随附 PNG',data:'来自 Data URL',https:'来自 HTTPS（打开时读取）',__asset:'来自 PNG 内嵌资源'})[result.avatar_source]||(result.avatar_source?'暂不支持的资源引用':'未声明主头像');
  modal('导入预览',`<div class="details-row"><span>名称</span><span>${escape(d.name)}</span></div><div class="details-row"><span>格式 / 角色版本</span><span>V3 / ${escape(d.character_version||'未设置')}</span></div><div class="details-row"><span>主头像</span><span>${escape(avatarHint)}</span></div><div class="details-row"><span>原始 PNG</span><span>${result.has_image?'已保留':'未附带'}</span></div>${result.avatar_warning?`<p class="small warning">${escape(result.avatar_warning)}</p>`:''}<div class="details-row"><span>世界书</span><span>${d.character_book?.entries?.length||0} 条 · 支持关键词与常驻触发</span></div><p class="small muted">V2 输入统一转换为 V3，保留角色版本。</p><div class="section">${result.compiled.warnings.map(w=>`<p class="small warning">${escape(w)}</p>`).join('')}</div><div class="dialog-footer">${state.current&&!state.current.locked&&!state.current.deleted?`<button data-action="import-update" data-import="${result.import_id}">更新当前「${escape(state.current.document.data.name)}」</button>`:''}<button data-action="import-create" data-import="${result.import_id}" class="primary">导入为新卡并绑定人格</button></div>`);
}));
$('#image-upload').addEventListener('change',()=>busy(async()=>{
  const input=$('#image-upload'), file=input.files[0], kind=input.dataset.kind;
  input.value='';
  if(!file || !state.current || !['card_image','avatar'].includes(kind))return;
  if(state.current.locked || state.current.deleted || state.current.sync.core_locked)throw Error('当前角色卡不可编辑。');
  if(file.size>20*1024*1024)throw Error('图片文件最大为 20 MiB。');
  const current=state.current;
  const result=await bridge.upload(kind==='avatar'?'upload-avatar':'upload-card-image',file);
  if(state.current!==current)return;
  if(!validImageUrl(result.data_url))throw Error('图片预览数据无效，请重新选择。');
  state.pendingImages[kind]=result;
  renderImagePanel();renderIdentity();updateSavebar();
  notice('图片已选择，点击“保存并同步”后生效。');
}));
let composingTags=false;
function handleTagInput(input) {
  if(!tagsEditable()||state.busy)return;
  state.tagDraft=input.value;
  if($('#tag-feedback'))$('#tag-feedback').textContent='';
  if(!composingTags&&/[、,，\r\n]/.test(input.value))commitTagDraft();
  else{updateTagControl();updateSavebar();}
}
document.addEventListener('compositionstart',event=>{if(worldbook.composition(event))return;if(event.target.id==='tag-input')composingTags=true;});
document.addEventListener('compositionend',event=>{
  if(worldbook.composition(event))return;
  if(event.target.id==='tag-input'){composingTags=false;handleTagInput(event.target);}
});
document.addEventListener('paste',event=>{
  if(worldbook.paste(event))return;
  if(event.target.id!=='tag-input'||!tagsEditable()||state.busy)return;
  const text=event.clipboardData?.getData('text/plain');
  if(!text||!/[、,，\r\n]/.test(text))return;
  event.preventDefault();
  const input=event.target, start=input.selectionStart??input.value.length, end=input.selectionEnd??start;
  state.tagDraft=input.value.slice(0,start)+text+input.value.slice(end);
  commitTagDraft();
});
document.addEventListener('focusout',event=>{
  if(event.target.id==='tag-input'&&!event.relatedTarget?.closest('.tag-editor')&&!composingTags)commitTagDraft();
});
document.addEventListener('input',event=>{
  const el=event.target;
  if(el.id==='delete-name'){updateDeleteControl();return;}
  if(!state.current)return;
  try{if(worldbook.input(event))return;}catch(error){notice(error.message,true);return;}
  if(el.id==='tag-input'){handleTagInput(el);return;}
  if(el.id==='raw-json'){state.raw=el.value;renderPreview();updateSavebar();return;}
  if(el.dataset.greeting){state.document.data[el.dataset.greeting][Number(el.dataset.index)]=el.value;}
  else if(el.dataset.field){
    const key=el.dataset.field,scope=el.dataset.scope;
    if(scope==='data')state.document.data[key]=el.value;
    if(scope==='options-number')state.options[key]=Number(el.value);
    if(scope==='options-null')state.options[key]=el.value||null;
    if(scope==='capability-list')state.options[key]=el.value.split('\n').map(s=>s.trim()).filter(Boolean);
  }else return;
  if(el.dataset.field==='name')renderIdentity();
  renderPreview();updateSavebar();
});
document.addEventListener('change',event=>{
  const el=event.target;
  if(el.id==='delete-persona'){updateDeleteControl();$('#delete-error').hidden=true;return;}
  if(el.id==='show-keys'){state.showKeys=el.checked;$('#studio').classList.toggle('show-keys',state.showKeys);}
  if(el.dataset.mode){state.options[el.dataset.mode]=el.value==='all'?null:el.value==='none'?[]:[''];renderPanel();updateSavebar();}
});
document.addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.confirm){$('#confirm-dialog').close(button.dataset.confirm);return;}
  if(state.busy)return;
  if(button.dataset.wbAction||button.dataset.wbSelect!==undefined){try{worldbook.click(button);}catch(error){notice(error.message,true);}return;}
  if(button.dataset.removeTag!==undefined){removeTag(Number(button.dataset.removeTag));return;}
  if(button.dataset.action==='add-tag'){commitTagDraft();$('#tag-input')?.focus();return;}
  if(button.dataset.action==='new-empty'){$('#new-button').click();return;}
  if(button.dataset.action==='import-empty'){$('#import-button').click();return;}
  if(button.dataset.action==='back'){busy(async()=>{if(await requireClean())await showCollection();});return;}
  if(button.dataset.action==='preview'){
    state.previewOpen=!state.previewOpen;
    $('#preview').hidden=!state.previewOpen;
    $('#editor-columns').classList.toggle('preview-open',state.previewOpen);
    const toggle=$('.save-actions [data-action="preview"]');
    toggle.setAttribute('aria-expanded',String(state.previewOpen));toggle.innerHTML=icon('panel')+(state.previewOpen?'收起预览':'运行预览');
    if(state.previewOpen){renderPreview();if(window.matchMedia('(max-width:999px)').matches)$('#preview').scrollIntoView({block:'start'});}
    else{clearTimeout(previewTimer);++previewRevision;toggle.focus();}
    return;
  }
  if(button.dataset.action==='discard'){busy(async()=>{if(await confirmAction('放弃当前未保存的修改？')){adopt(state.current);render();}});return;}
  if(button.dataset.imageUpload || button.dataset.imageDiscard || button.dataset.action==='avatar-follow'){
    if(!state.current || state.current.locked || state.current.deleted || state.current.sync.core_locked)return;
    if(button.dataset.imageUpload){const input=$('#image-upload');input.dataset.kind=button.dataset.imageUpload;input.click();return;}
    if(button.dataset.imageDiscard)delete state.pendingImages[button.dataset.imageDiscard];
    else state.pendingImages.avatar={upload_id:'ccdefault'};
    renderImagePanel();renderIdentity();updateSavebar();return;
  }
  if(button.dataset.card){busy(async()=>{if(await requireClean())await openCard(button.dataset.card);});return;}
  if(button.dataset.tab){
    worldbook.flush();
    const nextTab=button.dataset.tab==='more'?'astrbot':button.dataset.tab;
    const focusTab=()=>document.querySelector(`.tabs [data-tab="${activeTab()}"]`).focus({preventScroll:true});
    if(state.raw!==null && nextTab!=='source'){
      busy(async()=>{const result=await post('preview',{card:JSON.parse(state.raw),options:state.options});state.document=result.document;state.raw=null;state.tab=nextTab;render();focusTab();});return;
    }
    state.tab=nextTab;render();focusTab();return;
  }
  if((button.dataset.addGreeting || button.dataset.removeGreeting) && (state.current.locked || state.current.deleted))return;
  if(button.dataset.addGreeting){(state.document.data[button.dataset.addGreeting]??=[]).push('');renderPanel();updateSavebar();return;}
  if(button.dataset.removeGreeting){state.document.data[button.dataset.removeGreeting].splice(Number(button.dataset.index),1);renderPanel();renderPreview();updateSavebar();return;}
  if(button.dataset.snapshot){busy(async()=>{
    const result=await post('snapshot',{card_id:state.current.card_id,snapshot_id:button.dataset.snapshot});
    modal(`版本 ${result.character_version||'未设置'} · 与当前内容比较`,diffHtml({...state.document.data,'AstrBot options':state.options},{...result.snapshot.document.data,'AstrBot options':result.snapshot.options})+`<details class="section"><summary>查看完整快照</summary><pre>${escape(JSON.stringify(result.snapshot,null,2))}</pre></details><div class="dialog-footer"><button data-action="restore" data-id="${result.snapshot_id}" class="primary" ${state.current.locked?'disabled':''}>恢复此快照</button></div>`);
    applyDisabled();
  });return;}
  const action=button.dataset.action;
  if(action==='save'){busy(save);return;}
  if(action==='create-confirm'){busy(async()=>{
    const card=await post('create',{card:{spec:'chara_card_v3',spec_version:'3.0',data:{name:$('#new-name').value}},bind:$('#new-bind').checked});
    closeModal();state.deleted=false;state.tab='character';adopt(card);await loadList();render();notice(`角色卡已创建。${card.sync.error||''}`);window.scrollTo({top:0});
  });return;}
  if(action==='import-create'||action==='import-update'){busy(async()=>{
    if(!await requireClean())return;
    const updating=action==='import-update';
    if(updating&&!await confirmAction('用导入的完整卡片替换当前卡片？角色版本变化时会保存替换后的快照。'))return;
    const card=await post(updating?'update':'create',updating?{card_id:state.current.card_id,expected_revision:state.current.revision,import_id:button.dataset.import}:{import_id:button.dataset.import});
    closeModal();state.deleted=false;state.tab='character';adopt(card);await loadList();render();notice(`角色卡已${updating?'更新':'导入'}。${card.sync.error||''}`);window.scrollTo({top:0});
  });return;}
  if(action==='export'){
    modal('导出角色卡',`<div class="form"><label class="field">格式<select id="export-format"><option value="json">JSON</option><option value="png">PNG 人物卡</option></select></label><p class="small muted">统一导出为 Character Card V3。</p><p class="small muted">导出已保存内容${dirty()?'；当前尚有未保存修改':''}。</p></div><div class="dialog-footer"><button class="primary" data-action="export-confirm">下载角色卡</button></div>`);return;
  }
  if(action==='export-confirm'){busy(async()=>{const params={card_id:state.current.card_id,format:$('#export-format').value};await bridge.download('export',params);closeModal();});return;}
  if(action==='sync-diff'){
    const sync=state.current.sync;
    modal('人格同步差异',diffHtml({system_prompt:sync.current.system_prompt,begin_dialogs:sync.current.begin_dialogs},sync.desired)+`<div class="dialog-footer"><button class="primary" data-action="overwrite">用角色卡覆盖人格</button></div>`);return;
  }
  if(action==='overwrite'){busy(async()=>{await mutation('sync',{overwrite:true,expected_core_token:state.current.sync.expected_core_token});closeModal();});return;}
  if(action==='sync'){busy(()=>mutation('sync'));return;}
  if(action==='bind'){busy(()=>mutation('bind'));return;}
  if(action==='unbind'){busy(async()=>{if(await confirmAction('解除绑定后，角色卡和 AstrBot 人格都会保留。继续？'))await mutation('unbind');});return;}
  if(action==='lock'){busy(()=>mutation('lock',{locked:!state.current.locked}));return;}
  if(action==='archive'){busy(async()=>{if(await confirmAction('归档角色卡并停止卡片适配？当前内容、快照与 AstrBot 人格都会保留，可随时恢复到角色库。'))await mutation('archive');});return;}
  if(action==='unarchive'){busy(()=>mutation('unarchive'));return;}
  if(action==='delete'){if(!dirty())openDeleteDialog();return;}
  if(action==='cancel-delete'){closeModal();return;}
  if(action==='delete-confirm'){
    if($('#delete-name').value!==state.current.document.data.name)return;
    busy(async()=>{
      try {
        const result=await post('delete',{card_id:state.current.card_id,expected_revision:state.current.revision,confirm_name:$('#delete-name').value,delete_persona:$('#delete-persona')?.checked||false});
        closeModal();state.deleted=true;await showCollection();
        notice(`角色卡及其历史快照已永久删除，${result.persona_deleted?'绑定的 AstrBot 人格也已删除，聊天记录保留。':'AstrBot 人格与现有对话未受影响。'}${result.cleanup_pending?'部分图片暂未清理，将自动重试。':''}`);
      }catch(error){
        const message=$('#delete-error');
        if(!message||!$('#dialog').open)throw error;
        message.hidden=false;message.textContent=error.message||String(error);
      }
    });return;
  }
  if(action==='restore'){busy(async()=>{if(await confirmAction('恢复会覆盖当前卡片内容和 AstrBot 专用配置，不额外保存当前内容。继续？')){await mutation('restore',{snapshot_id:button.dataset.id});closeModal();}});}
});
window.addEventListener('beforeunload',event=>{if(dirty()){event.preventDefault();event.returnValue='';}});
document.addEventListener('keydown',event=>{
  if(worldbook.keydown(event))return;
  if(event.target.id==='tag-input'){
    if(event.isComposing||composingTags||event.keyCode===229||!tagsEditable()||state.busy)return;
    if(['Enter',',','，','、'].includes(event.key)){event.preventDefault();commitTagDraft();}
    else if(event.key==='Backspace'&&!event.target.value&&state.document.data.tags.length){event.preventDefault();removeTag(state.document.data.tags.length-1);}
    else if(event.key==='Escape'){event.preventDefault();state.tagDraft='';event.target.value='';updateTagControl();updateSavebar();}
    return;
  }
  if(!event.target.matches('.tabs [role="tab"]')||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  const buttons=[...document.querySelectorAll('.tabs [role="tab"]')], current=buttons.indexOf(event.target);
  const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(current+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;
  event.preventDefault(); buttons[next].click();
});

async function start() {
  document.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon);el.setAttribute('aria-hidden','true');});
  if(!bridge){notice('请从 AstrBot WebUI 的插件页面进入浮生绘卷。',true);$('#card-list').textContent='尚未连接';return;}
  bridge.onContext(context=>{document.documentElement.dataset.theme=context.isDark?'dark':'light';});
  await bridge.ready();
  await busy(async()=>{await loadList();render();});
}
start().catch(error=>notice(error.message,true));
