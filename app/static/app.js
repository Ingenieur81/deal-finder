const $ = (selector) => document.querySelector(selector);
let currentItems = [];

function text(value) { return value == null || value === '' ? '—' : String(value); }
function money(item) { const low = item.min_price, high = item.max_price, prefix = item.currency + ' '; return low && high ? `${prefix}${low}–${high}` : low ? `from ${prefix}${low}` : high ? `up to ${prefix}${high}` : 'Any price'; }
function when(value) { return value ? new Date(value).toLocaleString() : 'Never'; }
function notice(message, error = false) { const el = $('#notice'); el.textContent = message; el.style.color = error ? 'var(--danger)' : 'var(--accent)'; }
async function api(path, options = {}) { const response = await fetch(path, { headers:{'Content-Type':'application/json'}, ...options }); if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || `Request failed (${response.status})`); } return response.status === 204 ? null : response.json(); }

function row(item) {
  const tr = document.createElement('tr');
  const status = item.last_status || 'never';
  const target = item.notification_method === 'email' ? item.notification_target : 'Android FCM';
  tr.innerHTML = `<td><strong></strong><br><span class="muted"></span></td><td></td><td></td><td></td><td><span class="status"></span><br><small class="muted"></small></td><td></td><td class="actions"></td>`;
  tr.children[0].firstChild.textContent = item.name; tr.children[0].lastChild.textContent = item.enabled ? 'Enabled' : 'Paused';
  tr.children[1].textContent = money(item); tr.children[2].textContent = item.region; tr.children[3].textContent = target;
  const badge = tr.querySelector('.status'); badge.textContent = status.replace('_', ' '); badge.classList.add(status); tr.querySelector('small').textContent = item.last_error || '';
  tr.children[5].textContent = when(item.last_checked_at);
  [['Check now', () => check(item.id)], ['History', () => history(item)], ['Edit', () => edit(item)], ['Delete', () => removeItem(item)]].forEach(([label, fn]) => { const b=document.createElement('button'); b.textContent=label; if(label==='Delete') b.className='danger'; b.onclick=fn; tr.children[6].appendChild(b); });
  return tr;
}
async function load() { try { currentItems = await api('/api/items'); const body = $('#items'); body.replaceChildren(...currentItems.map(row)); $('#summary').textContent = `${currentItems.filter(x=>x.enabled).length} active watch${currentItems.length===1?'':'es'}`; } catch (e) { notice(e.message, true); } }
function formData() { const optional = id => $(id).value === '' ? null : Number($(id).value); return { name:$('#name').value, region:$('#region').value, min_price:optional('#min_price'), max_price:optional('#max_price'), currency:$('#currency').value, notification_method:$('#notification_method').value, notification_target:$('#notification_target').value, enabled:$('#enabled').checked }; }
$('#item-form').onsubmit = async event => { event.preventDefault(); try { const id=$('#item-id').value, data=formData(); await api(id ? `/api/items/${id}` : '/api/items', {method:id?'PUT':'POST', body:JSON.stringify(data)}); notice(id ? 'Watch updated; a new search has started.' : 'Watch created.'); resetForm(); load(); } catch(e) { notice(e.message, true); } };
function syncNotificationTarget() { const android=$('#notification_method').value==='android'; $('#target-label').firstChild.textContent=android?'FCM device token':'Email address'; $('#notification_target').type=android?'text':'email'; $('#notification_target').placeholder=android?'Firebase registration token':'you@example.com'; }
function resetForm() { $('#item-form').reset(); $('#currency').value='USD'; $('#enabled').checked=true; $('#item-id').value=''; $('#form-title').textContent='Add an item'; $('#cancel').hidden=true; syncNotificationTarget(); }
function edit(item) { $('#item-id').value=item.id; ['name','region','min_price','max_price','currency','notification_method','notification_target'].forEach(key => $( '#'+key ).value=item[key] ?? ''); $('#enabled').checked=item.enabled; syncNotificationTarget(); $('#form-title').textContent=`Edit: ${item.name}`; $('#cancel').hidden=false; window.scrollTo({top:0,behavior:'smooth'}); }
async function check(id) { notice('Searching…'); try { const result=await api(`/api/items/${id}/check`,{method:'POST'}); notice(`Check complete: ${result.eligible || 0} matching offer(s).`); load(); } catch(e) { notice(e.message,true); } }
async function removeItem(item) { if (!confirm(`Delete "${item.name}" and its price history?`)) return; try { await api(`/api/items/${item.id}`,{method:'DELETE'}); notice('Watch deleted.'); load(); } catch(e) { notice(e.message,true); } }
async function history(item) { try { const rows=await api(`/api/items/${item.id}/history`); $('#history-title').textContent=`Price history: ${item.name}`; const container=$('#history'); container.replaceChildren(); if(!rows.length) container.textContent='No prices recorded yet.'; rows.forEach(x=>{const d=document.createElement('div');d.className='history-row';const link=document.createElement('a');link.href=x.deal_url;link.target='_blank';link.rel='noopener';link.textContent='Open deal';d.append(`${x.currency} ${x.price} — ${x.retailer} — ${when(x.found_at)} `,link);container.appendChild(d);}); $('#history-dialog').showModal(); } catch(e) { notice(e.message,true); } }
$('#notification_method').onchange = syncNotificationTarget;
$('#cancel').onclick=resetForm; $('#refresh').onclick=load; $('#close-history').onclick=()=>$('#history-dialog').close(); load();
