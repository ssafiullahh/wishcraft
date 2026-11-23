const $ = id => document.getElementById(id);
const state = { occasions: [], lastWishRequest: null, lastMeta: null, lastDiscoveryMeta: null };
const localDate = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };
const addDays = (value, days) => { const d = new Date(`${value}T12:00:00`); d.setDate(d.getDate() + days); return d.toISOString().slice(0, 10); };
const prettyDate = value => new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(new Date(`${value}T12:00:00Z`));

function setStatus(id, message, error = false) {
  const el = $(id);
  el.textContent = message;
  el.classList.toggle('error', error);
}
function showPersonalize() { $('personalizeCard').classList.remove('hidden'); }
function setTone(value, dispatchChange = true) {
  const choices = [...document.querySelectorAll('.tone-choice')];
  if (!choices.some(choice => choice.dataset.tone === value)) return;
  $('tone').value = value;
  choices.forEach(choice => {
    const selected = choice.dataset.tone === value;
    choice.classList.toggle('selected', selected);
    choice.setAttribute('aria-pressed', String(selected));
  });
  if (dispatchChange) $('tone').dispatchEvent(new Event('change', { bubbles: true }));
}
document.querySelectorAll('.tone-choice').forEach(choice => {
  choice.addEventListener('click', () => setTone(choice.dataset.tone));
});
function toggleDemo() {
  const pill = $('modePill');
  pill.textContent = $('demo').checked ? 'Demo sample' : 'AI powered';
  pill.classList.toggle('demo-pill', $('demo').checked);
}
$('demo').addEventListener('change', toggleDemo);

$('discoverForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('discoverBtn');
  button.disabled = true;
  button.firstElementChild.textContent = 'Looking ahead…';
  setStatus('discoverStatus', '');
  const start = localDate();
  const end = addDays(start, Number($('period').value));
  const payload = {
    country: $('country').value.trim(), region: $('region').value.trim(),
    timezone: $('timezone').value.trim(), start_date: start, end_date: end,
    cultural_preferences: $('prefs').value.split(',').map(value => value.trim()).filter(Boolean),
    demo_mode: $('demo').checked,
  };
  try {
    let data;
    if (payload.demo_mode) {
      data = {
        country: payload.country, region: payload.region, timezone: payload.timezone,
        discovery_period: { start_date: start, end_date: end },
        occasions: [{ name: 'A Day Worth Celebrating (sample)', date: addDays(start, Math.min(3, Number($('period').value))), category: 'Sample', description: 'A fictional sample occasion.', relevance: 'medium', date_type: 'fixed', confidence: .5, verification_status: 'requires_verification' }],
        meta: { demo: true, provider: 'Demo mode', duration_ms: 0, validation: 'passed' },
      };
    } else {
      const response = await fetch('/api/v1/occasions/discover', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (!response.ok) throw new Error((await response.json()).detail || 'Could not find occasions.');
      data = await response.json();
    }
    state.occasions = data.occasions;
    state.lastDiscoveryMeta = data.meta;
    renderOccasions(data.occasions);
    const rejected = Number(data.meta?.rejected_count || 0);
    setStatus('discoverStatus', data.occasions.length
      ? `${data.occasions.length} dates · ${rejected} filtered`
      : `No valid dates${rejected ? ` · ${rejected} filtered` : ''}. Add one below.`);
  } catch (error) {
    setStatus('discoverStatus', error.message || 'Something went wrong. Try again.', true);
  } finally {
    button.disabled = false;
    button.firstElementChild.textContent = 'Discover occasions';
  }
});

function renderOccasions(occasions) {
  const section = $('occasionSection');
  const list = $('occasionList');
  section.classList.remove('hidden');
  list.replaceChildren();
  $('occasionCount').textContent = `· ${occasions.length}`;
  occasions.forEach(occasion => {
    const option = document.createElement('label');
    option.className = 'occasion-option';
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = 'occasionPick'; radio.value = occasion.name;
    radio.addEventListener('change', () => {
      // Concept 7: the person explicitly chooses the model's suggestion before generation.
      document.querySelectorAll('.occasion-option').forEach(row => row.classList.remove('selected'));
      option.classList.add('selected');
      $('occasion').value = occasion.name;
      $('customOccasion').value = '';
      showPersonalize();
    });
    const copy = document.createElement('span'); copy.className = 'occasion-copy';
    const name = document.createElement('span'); name.className = 'occasion-name'; name.textContent = occasion.name;
    const details = document.createElement('span'); details.className = 'occasion-detail';
    details.textContent = `${prettyDate(occasion.date)} · ${occasion.category} · ${occasion.description}`;
    copy.append(name, details);
    const tag = document.createElement('span');
    const uncertain = occasion.verification_status === 'requires_verification';
    tag.className = `tag ${uncertain ? 'uncertain' : ''}`;
    // Concept 6: confidence is model-reported, not source verification; never show it as a fact.
    tag.textContent = uncertain ? 'Check date' : 'Unverified';
    option.append(radio, copy, tag);
    list.append(option);
  });
}

$('customOccasion').addEventListener('input', () => {
  const value = $('customOccasion').value.trim();
  if (!value) return;
  $('occasion').value = value;
  document.querySelectorAll('[name="occasionPick"]').forEach(radio => { radio.checked = false; });
  document.querySelectorAll('.occasion-option').forEach(option => option.classList.remove('selected'));
  showPersonalize();
});

$('wishForm').addEventListener('submit', event => { event.preventDefault(); generateWish(); });
async function generateWish() {
  const button = $('generateBtn');
  button.disabled = true;
  button.firstElementChild.textContent = 'Writing…';
  setStatus('wishStatus', '');
  $('result').classList.add('hidden');
  const payload = {
    occasion: $('occasion').value.trim(), recipient_name: $('recipient').value.trim(),
    relationship: $('relationship').value, tone: $('tone').value, language: $('language').value,
    additional_context: $('context').value.trim(), demo_mode: $('demo').checked,
  };
  state.lastWishRequest = payload;
  try {
    let data;
    if (payload.demo_mode) {
      const name = payload.recipient_name ? ` ${payload.recipient_name}` : '';
      data = { wish: `Wishing you a wonderful ${payload.occasion}${name}! May your day be filled with happiness and memorable moments.`, language: payload.language, tone: payload.tone, meta: { demo: true, provider: 'Demo mode', duration_ms: 0 } };
    } else {
      const response = await fetch('/api/v1/wishes/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (!response.ok) throw new Error((await response.json()).detail || 'Could not write your wish.');
      data = await response.json();
    }
    $('wishText').value = data.wish;
    // Concept 7: the output remains editable; generation never sends or publishes it for the user.
    state.lastMeta = data.meta;
    $('wishBadge').textContent = data.meta.demo ? 'Demo sample' : 'Made for you';
    $('wishMeta').textContent = `${data.language} · ${data.tone}`;
    const rejected = state.lastDiscoveryMeta?.rejected_count ?? 0;
    $('transparencyText').textContent = `${$('country').value}, ${$('region').value} · ${$('timezone').value}. ${state.occasions.length} occasion(s); ${rejected} filtered by guardrails. Range checked; no calendar source connected. ${data.meta.provider || 'Configured model'}${data.meta.model ? ` · ${data.meta.model}` : ''} · ${data.meta.duration_ms ?? 0} ms.${data.meta.demo ? ' Sample only; not generated by an LLM.' : ''}`;
    $('result').classList.remove('hidden');
    setStatus('wishStatus', 'Ready to share.');
    $('result').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    setStatus('wishStatus', error.message || 'Something went wrong. Try again.', true);
  } finally {
    button.disabled = false;
    button.firstElementChild.textContent = 'Write my wish';
  }
}

$('regenBtn').addEventListener('click', generateWish);
$('copyBtn').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('wishText').value); }
  catch { $('wishText').select(); document.execCommand('copy'); }
  $('copyBtn').textContent = 'Copied';
  setTimeout(() => { $('copyBtn').textContent = 'Copy'; }, 1400);
});

fetch('/api/v1/preferences').then(response => response.ok ? response.json() : null).then(preferences => {
  if (preferences) {
    $('language').value = preferences.language; $('tone').value = preferences.tone;
    setTone(preferences.tone, false);
    $('country').value = preferences.country; $('region').value = preferences.region; $('timezone').value = preferences.timezone;
  }
}).catch(() => {});
for (const id of ['language', 'tone', 'country', 'region', 'timezone']) {
  $(id).addEventListener('change', () => {
    fetch('/api/v1/preferences', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ language: $('language').value, tone: $('tone').value, country: $('country').value, region: $('region').value, timezone: $('timezone').value }) }).catch(() => {});
  });
}
