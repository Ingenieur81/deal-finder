const $ = (selector) => document.querySelector(selector);
let currentItems = [];
let catalog = { countries: [], currencies: [] };
let historyItem = null;

function text(value) { return value == null || value === '' ? '—' : String(value); }
function money(item) { const low = item.min_price, high = item.max_price, prefix = item.currency + ' '; return low && high ? `${prefix}${low}–${high}` : low ? `from ${prefix}${low}` : high ? `up to ${prefix}${high}` : 'Any price'; }
function priceSummary(item) { const lowest = item.current_price == null ? 'No price found yet' : `Lowest: ${item.currency} ${item.current_price}${item.current_retailer ? ` (${item.current_retailer})` : ''}`; return `${money(item)}\n${lowest}`; }
function countryName(code) { return catalog.countries.find(country => country.code === code)?.name || code; }
function when(value) { return value ? new Date(value).toLocaleString() : 'Never'; }
function notice(message, error = false) { const el = $('#notice'); el.textContent = message; el.style.color = error ? 'var(--danger)' : 'var(--accent)'; }
async function api(path, options = {}) { const response = await fetch(path, { headers:{'Content-Type':'application/json'}, ...options }); if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || `Request failed (${response.status})`); } return response.status === 204 ? null : response.json(); }

function row(item) {
  const tr = document.createElement('tr');
  const status = item.last_status || 'never';
  const target = item.notification_method === 'email' ? item.notification_target : 'Android FCM';
  tr.innerHTML = `<td><strong></strong><br><span class="muted"></span></td><td></td><td></td><td></td><td><span class="status"></span><br><small class="muted"></small></td><td></td><td class="actions"></td>`;
  const itemName = tr.children[0].firstChild; if (item.current_deal_url) { const link=document.createElement('a'); link.href=item.current_deal_url; link.target='_blank'; link.rel='noopener'; link.textContent=item.name; itemName.replaceChildren(link); } else { itemName.textContent=item.name; } tr.children[0].lastChild.textContent = item.enabled ? 'Enabled' : 'Paused';
  tr.children[1].innerText = priceSummary(item); tr.children[2].textContent = countryName(item.region); tr.children[3].textContent = target;
  const badge = tr.querySelector('.status'); badge.textContent = status.replace('_', ' '); badge.classList.add(status); tr.querySelector('small').textContent = item.last_error || '';
  tr.children[5].textContent = when(item.last_checked_at);
  [['Check now', () => check(item.id)], ['History', () => history(item)], ['Edit', () => edit(item)], ['Delete', () => removeItem(item)]].forEach(([label, fn]) => { const b=document.createElement('button'); b.textContent=label; if(label==='Delete') b.className='danger'; b.onclick=fn; tr.children[6].appendChild(b); });
  return tr;
}
async function load() { try { currentItems = await api('/api/items'); const body = $('#items'); body.replaceChildren(...currentItems.map(row)); $('#summary').textContent = `${currentItems.filter(x=>x.enabled).length} active watch${currentItems.length===1?'':'es'}`; } catch (e) { notice(e.message, true); } }
function formData() { const optional = id => $(id).value === '' ? null : Number($(id).value); return { name:$('#name').value, region:$('#region').value, min_price:optional('#min_price'), max_price:optional('#max_price'), currency:$('#currency').value, notification_method:$('#notification_method').value, notification_target:$('#notification_target').value, enabled:$('#enabled').checked }; }
$('#item-form').onsubmit = async event => { event.preventDefault(); try { const id=$('#item-id').value, data=formData(); await api(id ? `/api/items/${id}` : '/api/items', {method:id?'PUT':'POST', body:JSON.stringify(data)}); notice(id ? 'Watch updated; a new search has started.' : 'Watch created.'); resetForm(); load(); } catch(e) { notice(e.message, true); } };
function syncNotificationTarget() { const android=$('#notification_method').value==='android'; $('#target-label').firstChild.textContent=android?'FCM device token':'Email address'; $('#notification_target').type=android?'text':'email'; $('#notification_target').placeholder=android?'Firebase registration token':'you@example.com'; }
function resetForm() { $('#item-form').reset(); $('#region').value='NL'; $('#currency').value='EUR'; $('#enabled').checked=true; $('#item-id').value=''; $('#form-title').textContent='Add an item'; $('#cancel').hidden=true; syncNotificationTarget(); }
function edit(item) { $('#item-id').value=item.id; ['name','region','min_price','max_price','currency','notification_method','notification_target'].forEach(key => $( '#'+key ).value=item[key] ?? ''); $('#enabled').checked=item.enabled; syncNotificationTarget(); $('#form-title').textContent=`Edit: ${item.name}`; $('#cancel').hidden=false; window.scrollTo({top:0,behavior:'smooth'}); }
async function check(id) { notice('Searching…'); try { const result=await api(`/api/items/${id}/check`,{method:'POST'}); notice(`Check complete: ${result.eligible || 0} matching offer(s).`); load(); } catch(e) { notice(e.message,true); } }
async function removeItem(item) { if (!confirm(`Delete "${item.name}" and its price history?`)) return; try { await api(`/api/items/${item.id}`,{method:'DELETE'}); notice('Watch deleted.'); load(); } catch(e) { notice(e.message,true); } }
function drawHistoryChart(rows, currency) {
  const canvas = $('#history-chart'), bounds = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(bounds.width * ratio)); canvas.height = Math.max(1, Math.floor(bounds.height * ratio));
  const context = canvas.getContext('2d'); context.scale(ratio, ratio);
  const width = bounds.width, height = bounds.height, padding = { top: 20, right: 18, bottom: 28, left: 58 };
  context.clearRect(0, 0, width, height); context.font = '12px system-ui';
  if (!rows.length) { context.fillStyle = '#aab8d0'; context.fillText('No prices recorded in this range.', padding.left, height / 2); return; }
  const points = [...rows].reverse().map(row => ({ price: Number(row.price), time: new Date(row.found_at).getTime() }));
  const prices = points.map(point => point.price), lowest = Math.min(...prices), highest = Math.max(...prices);
  const priceSpan = Math.max(highest - lowest, Math.max(highest * 0.05, 1));
  const floor = lowest - priceSpan * 0.1, ceiling = highest + priceSpan * 0.1;
  const times = points.map(point => point.time), start = Math.min(...times), end = Math.max(...times), timeSpan = Math.max(end - start, 86_400_000);
  const plotWidth = width - padding.left - padding.right, plotHeight = height - padding.top - padding.bottom;
  const x = point => padding.left + ((point.time - start) / timeSpan) * plotWidth;
  const y = point => padding.top + ((ceiling - point.price) / (ceiling - floor)) * plotHeight;
  context.strokeStyle = '#2b3b56'; context.fillStyle = '#aab8d0'; context.textAlign = 'right';
  [floor, (floor + ceiling) / 2, ceiling].forEach(value => { const lineY = padding.top + ((ceiling - value) / (ceiling - floor)) * plotHeight; context.beginPath(); context.moveTo(padding.left, lineY); context.lineTo(width - padding.right, lineY); context.stroke(); context.fillText(`${currency} ${value.toFixed(2)}`, padding.left - 6, lineY + 4); });
  context.strokeStyle = '#79dcb4'; context.lineWidth = 2; context.beginPath(); points.forEach((point, index) => index ? context.lineTo(x(point), y(point)) : context.moveTo(x(point), y(point))); context.stroke();
  context.fillStyle = '#79dcb4'; points.forEach(point => { context.beginPath(); context.arc(x(point), y(point), 3, 0, Math.PI * 2); context.fill(); });
  context.fillStyle = '#aab8d0'; context.textAlign = 'left'; context.fillText(new Date(start).toLocaleDateString(), padding.left, height - 7); context.textAlign = 'right'; context.fillText(new Date(end).toLocaleDateString(), width - padding.right, height - 7);
}
async function loadHistory() { if (!historyItem) return; try { const days=$('#history-range').value, rows=await api(`/api/items/${historyItem.id}/history?days=${days}`); $('#history-summary').textContent=rows.length ? `${rows.length} recorded price${rows.length===1?'':'s'} in the selected range.` : 'No prices recorded in the selected range.'; drawHistoryChart(rows, historyItem.currency); const container=$('#history'); container.replaceChildren(); rows.forEach(x=>{const d=document.createElement('div');d.className='history-row';const link=document.createElement('a');link.href=x.deal_url;link.target='_blank';link.rel='noopener';link.textContent='Open deal';d.append(`${x.currency} ${x.price} — ${x.retailer} — ${when(x.found_at)} `,link);container.appendChild(d);}); } catch(e) { notice(e.message,true); } }
async function history(item) { historyItem=item; $('#history-title').textContent=`Price history: ${item.name}`; $('#history-range').value='30'; $('#history-dialog').showModal(); await loadHistory(); }
function populateOptions() { $('#region').replaceChildren(...catalog.countries.map(country => new Option(country.name, country.code))); $('#currency').replaceChildren(...catalog.currencies.map(currency => new Option(currency, currency))); }
async function initialize() { try { catalog = await api('/api/options'); populateOptions(); resetForm(); await load(); } catch (e) { notice(e.message, true); } }
$('#notification_method').onchange = syncNotificationTarget;
$('#cancel').onclick=resetForm; $('#refresh').onclick=load; $('#close-history').onclick=()=>$('#history-dialog').close(); $('#history-range').onchange=loadHistory; initialize();
