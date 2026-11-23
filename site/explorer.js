(() => {
  const get = (id) => document.getElementById(id);
  const motion = matchMedia('(prefers-reduced-motion: reduce)');

  function playInView(container, control, name, interval, advance) {
    let enabled = !motion.matches;
    let visible = false;
    let timer = null;

    function sync() {
      const playing = enabled && visible && !document.hidden;
      if (playing && timer === null) timer = setInterval(advance, interval);
      if (!playing && timer !== null) {
        clearInterval(timer);
        timer = null;
      }
      control.textContent = (enabled ? 'Pause ' : 'Play ') + name;
      container.dataset.playing = String(playing);
    }

    function pause() {
      enabled = false;
      sync();
    }

    const observer = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting && entry.intersectionRatio >= .15;
      sync();
    }, { threshold: .15 });
    observer.observe(container);
    document.addEventListener('visibilitychange', sync);
    motion.addEventListener('change', () => { if (motion.matches) pause(); });
    control.addEventListener('click', () => { enabled = !enabled; sync(); });
    sync();
    return pause;
  }

  const source = 'https://github.com/fhajjej-ship-it/sfc-codex/blob/main/';
  const design = 'docs/CHAT-TRACKING.md';
  const skill = (name) => 'skills/' + name + '/SKILL.md';
  const nodes = {
    collect: [36, 52, 'Collect feedback', 'On demand', 'sfc-collect', 'Collect and verify reports before shaping a work ticket.', 'Public reports and optional connected sources.', 'Evidence and proposed tickets for triage.', skill('sfc-collect')],
    issue: [36, 166, 'GitHub issue', 'Input', 'Ready', 'Scope the work in Codex chat and link an issue with acceptance criteria.', 'User request and acceptance criteria.', 'A linked issue on your GitHub Project.', design],
    watch: [214, 166, 'Codex chat', 'Execution', 'your selected model', 'Build, test, and review the change you asked for in this chat. SFC syncs milestones to the board.', 'Your request, repo instructions, and current code.', 'Changes and verified milestones.', skill('sfc')],
    build: [392, 166, 'Build', 'In chat', 'sfc-build', 'Implement the requested change using the project’s usual workflow.', 'Issue scope and previous findings.', 'A candidate change for project checks.', skill('sfc-build')],
    checks: [570, 166, 'Project checks', 'Verification', 'test evidence', 'Run the relevant project checks and record the results.', 'Candidate source and configured check commands.', 'Passing receipts or actionable failures.', design],
    qa: [570, 52, 'QA', 'In chat', 'sfc-qa', 'Check the change against the acceptance criteria.', 'Candidate and project-check receipts.', 'Acceptance evidence or precise findings.', skill('sfc-qa')],
    security: [570, 280, 'Security', 'In chat', 'sfc-security', 'Inspect the candidate and its trust boundaries for security regressions.', 'Candidate changes and verification evidence.', 'Security verdict or actionable findings.', skill('sfc-security')],
    review: [748, 166, 'Final review', 'In chat', 'sfc-review', 'Review the exact candidate and completed evidence before it can merge.', 'Candidate, required checks, and review findings.', 'Review results and a linked PR when opened.', skill('sfc-review')],
    merge: [926, 166, 'Checked merge', 'Decision', 'reviewed PR', 'Merge through the repository’s normal process. SFC verifies the linked PR before it records Done.', 'Reviewed PR and exact head and base.', 'Merged change and completion evidence.', design],
    release: [926, 52, 'Release', 'Optional', 'package and publish', 'Publish through the repository’s release process when authorized.', 'Merged source, release policy, and applicable evidence.', 'Verified ZIP, checksum, and GitHub release.', design],
    visualize: [748, 280, 'Visual recap', 'On demand', 'sfc-visual', 'Build an HTML diagram or recap of the implementation and its evidence.', 'Source changes and available receipts.', 'A standalone visual artifact.', skill('sfc-visual')],
    sync: [214, 280, 'Git sync', 'On demand', 'sfc-git-sync', 'Review and integrate selected Git changes through a deliberate sync.', 'Selected changes and the remote branch.', 'Reviewed commits and an optional authorized push.', skill('sfc-git-sync')],
    refine: [392, 280, 'UI refine', 'On demand', 'sfc-ui-refine', 'Refine a specific page or component through browser review and focused edits.', 'A page, component, or visual request.', 'A focused implementation with visual checks.', skill('sfc-ui-refine')],
  };

  const map = get('map');
  const viewport = get('map-viewport');
  const scene = get('map-canvas');
  const mapScroll = get('map-scroll');
  const search = get('map-search');
  const zoomText = get('zoom-level');
  const mapResults = get('map-results');
  const drawing = get('map-lines');
  const width = drawing.width;
  const height = drawing.height;
  const buttons = new Map();
  const tour = ['issue', 'watch', 'build', 'checks', 'qa', 'security', 'review', 'merge', 'release'];
  let zoom = 1;
  let selected = 'issue';
  let tourIndex = 0;
  const pauseTour = playInView(map, get('play-map'), 'tour', 2400, () => {
    tourIndex = (tourIndex + 1) % tour.length;
    selectNode(tour[tourIndex], 'tour');
  });

  function zoomTo(value, keepCenter = true) {
    const centerX = (viewport.scrollLeft + viewport.clientWidth / 2) / zoom;
    const centerY = (viewport.scrollTop + viewport.clientHeight / 2) / zoom;
    zoom = Math.min(1.55, Math.max(.55, value));
    mapScroll.style.width = (width * zoom) + 'px';
    mapScroll.style.height = (height * zoom) + 'px';
    scene.style.transform = 'scale(' + zoom + ')';
    zoomText.value = Math.round(zoom * 100) + '%';
    get('zoom-out').disabled = zoom === .55;
    get('zoom-in').disabled = zoom === 1.55;
    viewport.scrollTo({
      left: keepCenter ? centerX * zoom - viewport.clientWidth / 2 : 0,
      top: keepCenter ? centerY * zoom - viewport.clientHeight / 2 : 0,
    });
  }

  function fitMap() {
    zoomTo(Math.min(1, viewport.clientWidth / width, viewport.clientHeight / height), false);
  }

  function selectNode(key, reveal = null) {
    const [, , title, type, command, description, input, output, path] = nodes[key];
    buttons.get(selected).setAttribute('aria-pressed', 'false');
    const node = buttons.get(key);
    node.setAttribute('aria-pressed', 'true');
    selected = key;
    get('node-type').textContent = type + ' · ' + command;
    const heading = get('node-title');
    heading.setAttribute('aria-live', reveal === 'tour' ? 'off' : 'polite');
    heading.textContent = title;
    get('node-description').textContent = description;
    get('node-input').textContent = input;
    get('node-output').textContent = output;
    get('node-link').href = source + path;
    if (reveal === 'search') node.focus({ preventScroll: true });
    if (reveal && viewport.scrollWidth > viewport.clientWidth) {
      const [x, y] = nodes[key];
      viewport.scrollTo({ left: (x + 72) * zoom - viewport.clientWidth / 2,
        top: (y + 36) * zoom - viewport.clientHeight / 2,
        behavior: reveal === 'tour' && !motion.matches ? 'smooth' : 'instant' });
    }
  }

  for (const [key, [x, y, title, type, command]] of Object.entries(nodes)) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'map-node';
    node.dataset.kind = type;
    node.style.left = x + 'px';
    node.style.top = y + 'px';
    node.setAttribute('aria-pressed', 'false');
    node.setAttribute('aria-controls', 'node-title');
    const name = document.createElement('strong');
    name.textContent = title;
    const label = document.createElement('span');
    label.textContent = command;
    node.append(name, label);
    node.addEventListener('click', () => { pauseTour(); selectNode(key); });
    scene.append(node);
    buttons.set(key, node);
  }

  function route(points, optional = false) {
    const context = drawing.getContext('2d');
    const [[startX, startY], ...rest] = points;
    context.beginPath();
    context.moveTo(startX, startY);
    for (const [x, y] of rest) context.lineTo(x, y);
    context.strokeStyle = optional ? '#949d77' : '#78ae96';
    context.lineWidth = 1.8;
    context.setLineDash(optional ? [4, 5] : []);
    context.stroke();

    const [x, y] = points.at(-1);
    const [previousX, previousY] = points.at(-2);
    const angle = Math.atan2(y - previousY, x - previousX);
    context.beginPath();
    context.moveTo(x, y);
    for (const direction of [-1, 1]) {
      const wing = angle + direction * .55;
      context.lineTo(x - Math.cos(wing) * 9, y - Math.sin(wing) * 9);
    }
    context.closePath();
    context.fillStyle = context.strokeStyle;
    context.fill();
  }

  for (const x of [36, 214, 392, 748]) route([[x + 144, 202], [x + 178, 202]]);
  route([[642, 166], [642, 124]]);
  route([[642, 238], [642, 280]]);
  route([[714, 88], [731, 88], [731, 182], [748, 182]]);
  route([[714, 316], [731, 316], [731, 223], [748, 223]]);
  route([[108, 124], [108, 166]], true);
  route([[998, 166], [998, 124]], true);
  route([[286, 238], [286, 280]], true);
  route([[464, 238], [464, 280]], true);
  route([[820, 238], [820, 280]], true);

  function searchMap() {
    const term = search.value.toLowerCase().trim();
    const matches = [];
    for (const [key, node] of buttons) {
      const match = nodes[key].slice(2, 6).join(' ').toLowerCase().includes(term);
      node.classList.toggle('match', Boolean(term) && match);
      node.classList.toggle('dimmed', !match);
      if (match) matches.push(key);
    }
    mapResults.textContent = term
      ? (matches.length ? matches.length + ' matching steps. Press Enter to open the first.' : 'No matching skills or stages.')
      : 'Select a node to explore.';
    return matches;
  }
  search.addEventListener('focus', pauseTour);
  search.addEventListener('input', searchMap);
  search.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    const [first] = searchMap();
    if (first) selectNode(first, 'search');
  });
  get('zoom-out').addEventListener('click', () => { pauseTour(); zoomTo(zoom - .15); });
  get('zoom-in').addEventListener('click', () => { pauseTour(); zoomTo(zoom + .15); });
  get('fit-map').addEventListener('click', () => { pauseTour(); fitMap(); });
  viewport.addEventListener('keydown', pauseTour);
  viewport.addEventListener('wheel', pauseTour, { passive: true });
  const fullscreen = get('expand-map');
  if (document.fullscreenEnabled) {
    fullscreen.hidden = false;
    fullscreen.addEventListener('click', async () => {
      try {
        if (document.fullscreenElement) await document.exitFullscreen();
        else await map.requestFullscreen();
      } catch {
        mapResults.textContent = 'Full screen is unavailable in this browser.';
      }
    });
    document.addEventListener('fullscreenchange', () => {
      fullscreen.textContent = document.fullscreenElement ? 'Exit full screen' : 'Full screen';
      fitMap();
    });
  }

  let drag;
  viewport.addEventListener('pointerdown', (event) => {
    pauseTour();
    const onScrollbar = event.target === viewport &&
      (event.offsetX >= viewport.clientWidth || event.offsetY >= viewport.clientHeight);
    if (event.pointerType !== 'mouse' || event.button !== 0 || event.target.closest('button') || onScrollbar) return;
    drag = { x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop };
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add('dragging');
  });
  viewport.addEventListener('pointermove', (event) => {
    if (drag) viewport.scrollTo(drag.left + drag.x - event.clientX, drag.top + drag.y - event.clientY);
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    viewport.addEventListener(type, () => {
      drag = null;
      viewport.classList.remove('dragging');
    });
  }
  map.hidden = false;
  selectNode(selected);
  fitMap();

  const titles = {
    export: 'Add CSV export',
    retry: 'Retry failed sync',
    access: 'Set export permissions',
    keyboard: 'Improve keyboard navigation',
    guide: 'Update the workspace guide',
  };
  const queued = ['Ready', 'Scope ready', 'The issue has acceptance criteria. Work starts when you ask in Codex chat.'];
  const building = ['Building', 'Chat starts work', 'Codex implements the requested change in chat.'];
  const qa = ['QA', 'Project checks and QA', 'Configured checks produce test evidence. QA verifies acceptance criteria and Security checks relevant risks.'];
  const review = ['Review', 'Review in chat', 'Codex reviews the change and the completed checks.'];
  const merged = ['Done', 'Verified merge', 'The PR merges through the repository’s normal process. SFC verifies that merge and records Done.'];

  const frames = [
    ['Queue ready', {
      export: queued, retry: queued, access: queued, keyboard: queued,
      guide: ['Backlog', 'Needs refinement', 'The guide stays in Backlog while its scope is prepared.'],
    }],
    ['Two builds start', { export: building, retry: building }],
    ['Checks start', { export: qa, retry: qa, access: building }],
    ['Finding and blocker', {
      export: ['QA', 'QA finds an edge case', 'A quoted comma breaks CSV output. QA records the finding on the current candidate.'],
      retry: review,
      access: ['Blocked', 'Maintainer decision needed', 'The export permission policy needs a human decision. Other tickets continue.'],
    }],
    ['Repair and merge', {
      export: ['Building', 'Same PR repaired', 'Codex fixes the CSV edge case in chat and updates the same PR.'],
      retry: merged,
      keyboard: building,
    }],
    ['Parallel checks', {
      export: ['QA', 'Affected checks revalidated', 'Codex reruns the relevant checks and SFC records the results.'],
      keyboard: qa,
    }],
    ['Parallel review', { export: review, keyboard: review }],
    ['CSV export merged', { export: merged }],
    ['Keyboard change merged', { keyboard: merged }],
  ];
  const snapshots = [];
  let snapshot = {};
  for (const [, updates] of frames) {
    snapshot = { ...snapshot, ...updates };
    snapshots.push(snapshot);
  }

  const demo = get('demo');
  const board = get('demo-board');
  const lanes = new Map();
  for (const name of ['Backlog', 'Ready', 'Building', 'QA', 'Review', 'Done', 'Blocked']) {
    const lane = document.createElement('section');
    lane.className = 'demo-lane';
    lane.dataset.lane = name;
    const title = document.createElement('h3');
    const count = document.createElement('span');
    title.textContent = name;
    title.append(count);
    const list = document.createElement('div');
    list.className = 'demo-cards';
    lane.append(title, list);
    board.append(lane);
    lanes.set(name, { lane, list, count });
  }

  const cards = new Map();
  const timeline = get('demo-timeline');
  const previous = get('back-demo');
  const next = get('next-demo');
  let index = 0;
  let selectedTicket = 'export';
  let holdEnd = true;

  const pauseDemo = playInView(demo, get('play-demo'), 'demo', 1900, () => {
    if (index === frames.length - 1 && holdEnd) {
      holdEnd = false;
      return;
    }
    showFrame((index + 1) % frames.length);
  });

  function inspectTicket(fromPlayback) {
    const [lane, heading, detail] = snapshots[index][selectedTicket];
    get('demo-ticket').textContent = titles[selectedTicket] + ' · ' + lane;
    get('demo-stage').textContent = heading;
    const description = get('demo-detail');
    description.setAttribute('aria-live', fromPlayback ? 'off' : 'polite');
    description.textContent = detail;
    for (const [key, card] of cards) card.setAttribute('aria-pressed', String(key === selectedTicket));
  }

  for (const [key, title] of Object.entries(titles)) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'demo-card';
    card.dataset.ticket = key;
    card.setAttribute('aria-controls', 'demo-stage demo-detail');
    const name = document.createElement('strong');
    name.textContent = title;
    card.append(name, document.createElement('span'));
    card.addEventListener('click', () => {
      pauseDemo();
      selectedTicket = key;
      inspectTicket(false);
    });
    cards.set(key, card);
  }

  function visibleBounds(card) {
    const bounds = card.getBoundingClientRect();
    const list = card.parentElement.getBoundingClientRect();
    return bounds.top >= list.top && bounds.bottom <= list.bottom + 1 ? bounds : null;
  }

  function animateCardMove(card, before, after, boardBounds) {
    const dx = before.left - after.left;
    const dy = before.top - after.top;
    if (!dx && !dy) return;

    // Draw over the board so movement between scrollable lanes stays visible.
    const moving = card.cloneNode(true);
    moving.className = 'demo-card-motion';
    delete moving.dataset.ticket;
    moving.inert = true;
    moving.setAttribute('aria-hidden', 'true');
    const colors = getComputedStyle(card);
    for (const property of ['--lane', '--lane-bg']) {
      moving.style.setProperty(property, colors.getPropertyValue(property));
    }
    Object.assign(moving.style, {
      left: (after.left - boardBounds.left) + 'px',
      top: (after.top - boardBounds.top) + 'px',
      width: after.width + 'px',
    });
    board.append(moving);
    const timing = { duration: 420, easing: 'ease-out' };
    card.animate([{ opacity: 0 }, { opacity: 0 }], timing);
    const motion = moving.animate([
      { transform: 'translate(' + dx + 'px, ' + dy + 'px)' }, { transform: 'none' },
    ], timing);
    motion.finished.then(() => moving.remove(), () => moving.remove());
  }

  function showFrame(target, fromPlayback = true) {
    index = target;
    holdEnd = true;
    const state = snapshots[index];
    const positions = new Map();
    for (const moving of board.querySelectorAll('.demo-card-motion')) moving.remove();
    if (!motion.matches) {
      for (const [key, card] of cards) {
        if (card.isConnected) {
          for (const animation of card.getAnimations()) animation.cancel();
          const bounds = visibleBounds(card);
          if (bounds) positions.set(key, bounds);
        }
      }
    }

    const totals = new Map([...lanes.keys()].map((lane) => [lane, 0]));
    for (const [key, card] of cards) {
      const [name, title] = state[key];
      const { list } = lanes.get(name);
      if (card.parentElement !== list) list.append(card);
      card.lastElementChild.textContent = title;
      totals.set(name, totals.get(name) + 1);
    }
    for (const [name, { lane, count }] of lanes) {
      count.textContent = totals.get(name);
      lane.classList.toggle('active', totals.get(name) > 0);
    }
    const boardBounds = board.getBoundingClientRect();
    for (const [key, before] of positions) {
      const card = cards.get(key);
      const after = visibleBounds(card);
      if (after) animateCardMove(card, before, after, boardBounds);
    }

    const active = totals.get('Building') + totals.get('QA') + totals.get('Review');
    get('demo-summary').textContent = active + ' active · ' + totals.get('Done') + ' merged · ' + totals.get('Blocked') + ' blocked';
    get('demo-progress').textContent = 'Step ' + (index + 1) + ' of ' + frames.length;
    for (const [position, item] of [...timeline.children].entries()) {
      item.firstElementChild.setAttribute('aria-current', position === index ? 'step' : 'false');
    }
    previous.disabled = index === 0;
    next.disabled = index === frames.length - 1;
    inspectTicket(fromPlayback);
  }

  function seek(target) {
    pauseDemo();
    showFrame(target, false);
  }

  for (const [position, [label]] of frames.entries()) {
    const item = document.createElement('li');
    const step = document.createElement('button');
    step.type = 'button';
    step.textContent = label;
    step.addEventListener('click', () => seek(position));
    item.append(step);
    timeline.append(item);
  }

  previous.addEventListener('click', () => seek(index - 1));
  next.addEventListener('click', () => seek(index + 1));
  get('reset-demo').addEventListener('click', () => seek(0));
  demo.hidden = false;
  showFrame(0);
})();
