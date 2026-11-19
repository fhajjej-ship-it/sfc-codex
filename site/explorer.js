(() => {
  const get = (id) => document.getElementById(id);
  const source = 'https://github.com/fhajjej-ship-it/sfc-codex/blob/main/';
  const design = 'docs/DESIGN.md';
  const skill = (name) => 'skills/' + name + '/SKILL.md';
  const nodes = {
    collect: [36, 52, 'Collect feedback', 'On demand', 'sfc-collect', 'Collect and verify reports before shaping a work ticket.', 'Public reports and optional connected sources.', 'Evidence and proposed tickets for triage.', skill('sfc-collect')],
    issue: [36, 166, 'GitHub issue', 'Input', 'Ready', 'Scope the work with acceptance criteria and make it eligible on the project board.', 'User outcome and acceptance criteria.', 'An eligible issue on your GitHub Project.', design],
    watch: [214, 166, 'Supervisor', 'Coordinator', 'sfc', 'Claim eligible tickets, schedule work, and check evidence before changing GitHub state.', 'Issue, project policy, and saved run state.', 'A worktree and pinned candidate for the next lane.', skill('sfc')],
    build: [392, 166, 'Build', 'Factory lane', 'sfc-build', 'Implement the issue in a separate worktree and prepare the candidate for verification.', 'Issue scope and previous findings.', 'A candidate change for project checks.', skill('sfc-build')],
    checks: [570, 166, 'Project checks', 'Verification', 'test evidence', 'Run configured checks and reuse valid results when their inputs and policy match.', 'Candidate source and configured check commands.', 'Passing receipts or actionable failures.', 'docs/VERIFICATION-REUSE.md'],
    qa: [570, 52, 'QA', 'Factory lane', 'sfc-qa', 'Independently assess the candidate against every acceptance criterion.', 'Candidate and project-check receipts.', 'Acceptance evidence or precise findings.', skill('sfc-qa')],
    security: [570, 280, 'Security', 'Factory lane', 'sfc-security', 'Inspect the candidate and its trust boundaries for security regressions.', 'Candidate changes and verification evidence.', 'Security verdict or actionable findings.', skill('sfc-security')],
    review: [748, 166, 'Final review', 'Factory lane', 'sfc-review', 'Review the exact candidate and completed evidence before it can merge.', 'Candidate, required checks, and review findings.', 'An independent review verdict.', skill('sfc-review')],
    merge: [926, 166, 'Checked merge', 'Decision', 'reviewed PR', 'Confirm the candidate and required checks are current. Merge only with repository authority.', 'Reviewed PR and exact head and base.', 'Merged change and completion evidence.', design],
    release: [926, 52, 'Release', 'Optional', 'package and publish', 'Prepare a versioned package from merged source. Publication requires the configured authority.', 'Merged source, release policy, and applicable evidence.', 'Verified ZIP, checksum, and GitHub release.', design],
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
  let zoom = 1;
  let selected = 'issue';

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

  function selectNode(key, reveal = false) {
    const [, , title, type, command, description, input, output, path] = nodes[key];
    buttons.get(selected).setAttribute('aria-pressed', 'false');
    const node = buttons.get(key);
    node.setAttribute('aria-pressed', 'true');
    selected = key;
    get('node-type').textContent = type + ' · ' + command;
    get('node-title').textContent = title;
    get('node-description').textContent = description;
    get('node-input').textContent = input;
    get('node-output').textContent = output;
    get('node-link').href = source + path;
    if (reveal) {
      node.focus({ preventScroll: true });
      const [x, y] = nodes[key];
      viewport.scrollTo({ left: (x + 72) * zoom - viewport.clientWidth / 2,
        top: (y + 36) * zoom - viewport.clientHeight / 2 });
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
    node.addEventListener('click', () => selectNode(key));
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
  search.addEventListener('input', searchMap);
  search.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    const [first] = searchMap();
    if (first) selectNode(first, true);
  });
  get('zoom-out').addEventListener('click', () => zoomTo(zoom - .15));
  get('zoom-in').addEventListener('click', () => zoomTo(zoom + .15));
  get('fit-map').addEventListener('click', fitMap);
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

  const ready = ['Ready', 'Issue ready', 'The issue has acceptance criteria and is eligible for pickup.'];
  const building = ['Building', 'Worktree created', 'The supervisor assigns the ticket and Codex implements the change in an isolated worktree.'];
  const qa = ['QA', 'Project checks and QA', 'SFC records the configured test results. QA checks acceptance criteria and Security examines relevant risks.'];
  const review = ['Review', 'Independent review', 'The reviewer checks the candidate and completed evidence at the same commit.'];
  const done = ['Done', 'Verified merge', 'With merge authority, SFC verifies the current candidate and required checks, merges the PR, and marks the ticket Done.'];
  const scenarios = {
    clean: [ready, building, qa, review, done],
    repair: [ready, building, qa,
      ['QA', 'QA finds an edge case', 'The exported CSV drops quoted commas. QA records the finding against the current candidate.'],
      ['Building', 'Same PR repaired', 'An eligible repair updates the same PR within its attempt budget.'],
      ['QA', 'Affected checks revalidated', 'SFC runs affected checks and reuses passing evidence only where its inputs still match.'],
      review, done],
    blocked: [ready, building,
      ['Blocked', 'Maintainer decision needed', 'The ticket requires a product decision about export permissions. It waits here for a human response while other eligible tickets can continue.']],
  };
  const lanes = new Map();
  const board = get('demo-board');
  for (const name of ['Backlog', 'Ready', 'Building', 'QA', 'Review', 'Done', 'Blocked']) {
    const lane = document.createElement('section');
    lane.className = 'demo-lane';
    lane.dataset.lane = name;
    const label = document.createElement('h3');
    label.textContent = name;
    lane.append(label);
    board.append(lane);
    lanes.set(name, lane);
  }
  const card = document.createElement('div');
  card.className = 'demo-card';
  card.textContent = 'Add CSV export';
  const scenario = get('scenario');
  const play = get('play-demo');
  const back = get('back-demo');
  const next = get('next-demo');
  const timeline = get('demo-timeline');
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  let steps = scenarios.clean;
  let index = 0;
  let timer = null;

  function pause() {
    clearInterval(timer);
    timer = null;
    play.textContent = 'Play';
  }

  function showStep(target) {
    index = target;
    const [stage, label, description] = steps[index];
    const lane = lanes.get(stage);
    const before = card.parentElement ? card.getBoundingClientRect() : null;
    for (const item of lanes.values()) item.classList.toggle('active', item === lane);
    lane.append(card);
    if (before && !motion.matches) {
      const after = card.getBoundingClientRect();
      if (before.left !== after.left || before.top !== after.top) {
        for (const animation of card.getAnimations()) animation.cancel();
        card.animate([{ transform: 'translate(' + (before.left - after.left) + 'px, ' +
          (before.top - after.top) + 'px)' }, { transform: 'none' }], { duration: 280, easing: 'ease-out' });
      }
    }
    get('demo-progress').textContent = 'Step ' + (index + 1) + ' of ' + steps.length;
    get('demo-stage').textContent = label;
    get('demo-detail').textContent = description;
    for (const [position, item] of [...timeline.children].entries()) {
      item.firstElementChild.setAttribute('aria-current', position === index ? 'step' : 'false');
    }
    back.disabled = index === 0;
    next.disabled = index === steps.length - 1;
    if (next.disabled) {
      pause();
      play.textContent = 'Replay';
    } else if (timer === null) {
      play.textContent = 'Play';
    }
  }

  function chooseScenario() {
    pause();
    steps = scenarios[scenario.value];
    timeline.replaceChildren();
    for (const [position, [, label]] of steps.entries()) {
      const item = document.createElement('li');
      const step = document.createElement('button');
      step.type = 'button';
      step.textContent = label;
      step.addEventListener('click', () => {
        pause();
        showStep(position);
      });
      item.append(step);
      timeline.append(item);
    }
    showStep(0);
  }

  scenario.addEventListener('change', chooseScenario);
  play.addEventListener('click', () => {
    if (timer !== null) {
      pause();
      return;
    }
    if (index === steps.length - 1) showStep(0);
    play.textContent = 'Pause';
    timer = setInterval(() => showStep(index + 1), 1800);
  });
  back.addEventListener('click', () => { pause(); showStep(index - 1); });
  next.addEventListener('click', () => { pause(); showStep(index + 1); });
  get('reset-demo').addEventListener('click', () => { pause(); showStep(0); });
  get('demo').hidden = false;
  chooseScenario();
})();
