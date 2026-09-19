/* Offline force-directed graph with pan, zoom, and rotate. */

(function () {
  const WORLD = 1400;
  const CX = WORLD / 2;
  const CY = WORLD / 2;

  function layout(nodes, edges) {
    const placed = nodes.map((node, index) => {
      const angle = (index / Math.max(nodes.length, 1)) * Math.PI * 2;
      const ring = node.group === 'person' ? 0 : 180 + (index % 6) * 36;
      return {
        ...node,
        x: CX + Math.cos(angle) * ring,
        y: CY + Math.sin(angle) * ring,
        r: node.group === 'person' ? 22 : node.group === 'event' ? 8 : 12 + Math.min(10, (node.weight || 0) * 1.6),
      };
    });
    const byId = Object.fromEntries(placed.map((node) => [node.id, node]));
    const links = edges
      .map((edge) => ({ ...edge, a: byId[edge.from], b: byId[edge.to] }))
      .filter((edge) => edge.a && edge.b);

    for (let step = 0; step < 90; step += 1) {
      for (let i = 0; i < placed.length; i += 1) {
        for (let j = i + 1; j < placed.length; j += 1) {
          const a = placed[i];
          const b = placed[j];
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          const dist = Math.hypot(dx, dy) || 0.1;
          const force = 1800 / (dist * dist);
          dx = (dx / dist) * force;
          dy = (dy / dist) * force;
          a.x += dx;
          a.y += dy;
          b.x -= dx;
          b.y -= dy;
        }
      }
      for (const link of links) {
        const dx = link.b.x - link.a.x;
        const dy = link.b.y - link.a.y;
        const dist = Math.hypot(dx, dy) || 0.1;
        const pull = (dist - 130) * 0.035;
        link.a.x += (dx / dist) * pull;
        link.a.y += (dy / dist) * pull;
        link.b.x -= (dx / dist) * pull;
        link.b.y -= (dy / dist) * pull;
      }
      for (const node of placed) {
        node.x += (CX - node.x) * 0.015;
        node.y += (CY - node.y) * 0.015;
        node.x = Math.min(WORLD - 40, Math.max(40, node.x));
        node.y = Math.min(WORLD - 40, Math.max(40, node.y));
      }
    }
    return { nodes: placed, links };
  }

  window.MemoryGraphView = function MemoryGraphView(canvas, onSelect) {
    const ctx = canvas.getContext('2d');
    let model = { nodes: [], links: [] };
    let selectedId = null;
    let data = { nodes: [], edges: [] };
    let scale = 1;
    let panX = 0;
    let panY = 0;
    let angle = 0;
    let dragging = false;
    let rotating = false;
    let moved = false;
    let lastX = 0;
    let lastY = 0;

    function resize() {
      const parent = canvas.parentElement;
      const rect = parent.getBoundingClientRect();
      const width = Math.max(240, Math.floor(rect.width));
      const height = Math.max(240, Math.floor(rect.height));
      if (canvas.width === width && canvas.height === height) return;
      canvas.width = width;
      canvas.height = height;
    }

    function canvasPoint(sx, sy) {
      const rect = canvas.getBoundingClientRect();
      return {
        x: (sx - rect.left) * (canvas.width / rect.width),
        y: (sy - rect.top) * (canvas.height / rect.height),
      };
    }

    function rotatePoint(x, y, theta) {
      const dx = x - CX;
      const dy = y - CY;
      const c = Math.cos(theta);
      const s = Math.sin(theta);
      return { x: dx * c - dy * s + CX, y: dx * s + dy * c + CY };
    }

    function worldToCamera(wx, wy) {
      return rotatePoint(wx, wy, angle);
    }

    function screenToWorld(sx, sy) {
      const p = canvasPoint(sx, sy);
      const camX = (p.x - panX) / scale;
      const camY = (p.y - panY) / scale;
      return rotatePoint(camX, camY, -angle);
    }

    function applyWorldTransform() {
      ctx.setTransform(scale, 0, 0, scale, panX, panY);
      ctx.translate(CX, CY);
      ctx.rotate(angle);
      ctx.translate(-CX, -CY);
    }

    function fit() {
      resize();
      angle = 0;
      scale = Math.min(canvas.width / WORLD, canvas.height / WORLD) * 0.92;
      panX = (canvas.width - WORLD * scale) / 2;
      panY = (canvas.height - WORLD * scale) / 2;
    }

    function redraw() {
      resize();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#0e0f13';
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      applyWorldTransform();
      for (const link of model.links) {
        ctx.beginPath();
        ctx.moveTo(link.a.x, link.a.y);
        ctx.lineTo(link.b.x, link.b.y);
        ctx.strokeStyle = 'rgba(91, 157, 255, 0.38)';
        ctx.lineWidth = Math.max(1, link.width || 1) / scale;
        ctx.stroke();
      }
      for (const node of model.nodes) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
        ctx.fillStyle = node.color || '#8b93a3';
        ctx.fill();
        if (node.id === selectedId) {
          ctx.strokeStyle = '#e7e9ee';
          ctx.lineWidth = 2 / scale;
          ctx.stroke();
        }
      }

      ctx.setTransform(1, 0, 0, 1, 0, 0);
      for (const node of model.nodes) {
        const cam = worldToCamera(node.x, node.y);
        const sx = cam.x * scale + panX;
        const sy = cam.y * scale + panY + node.r * scale + 4;
        ctx.fillStyle = '#e7e9ee';
        ctx.font = `${node.group === 'person' ? 700 : 500} ${node.group === 'event' ? 11 : 13}px system-ui`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(node.label, sx, sy);
      }
    }

    function hit(world) {
      return [...model.nodes].reverse().find((node) => Math.hypot(node.x - world.x, node.y - world.y) <= node.r + 6);
    }

    function wantsRotate(event) {
      return event.shiftKey || event.altKey || event.button === 2 || event.buttons === 2;
    }

    this.render = function render(payload, options) {
      data = payload || { nodes: [], edges: [] };
      model = layout(data.nodes || [], data.edges || []);
      const keepView = options && options.keepView && canvas.width;
      if (!keepView) fit();
      redraw();
      const person = (data.nodes || []).find((node) => node.group === 'person');
      if (person && onSelect) onSelect(person, data);
    };

    this.rotateBy = function rotateBy(radians) {
      angle += radians;
      redraw();
    };

    this.resetView = function resetView() {
      fit();
      redraw();
    };

    canvas.addEventListener('wheel', (event) => {
      event.preventDefault();
      if (event.shiftKey) {
        angle += event.deltaY * 0.004;
        redraw();
        return;
      }
      const world = screenToWorld(event.clientX, event.clientY);
      const next = Math.min(3.2, Math.max(0.35, scale * (event.deltaY < 0 ? 1.12 : 0.89)));
      const p = canvasPoint(event.clientX, event.clientY);
      const cam = worldToCamera(world.x, world.y);
      panX = p.x - cam.x * next;
      panY = p.y - cam.y * next;
      scale = next;
      redraw();
    }, { passive: false });

    canvas.addEventListener('contextmenu', (event) => event.preventDefault());

    canvas.addEventListener('pointerdown', (event) => {
      dragging = true;
      rotating = wantsRotate(event);
      moved = false;
      lastX = event.clientX;
      lastY = event.clientY;
      canvas.classList.toggle('rotating', rotating);
      canvas.setPointerCapture(event.pointerId);
    });

    canvas.addEventListener('pointermove', (event) => {
      if (!dragging) return;
      const dx = event.clientX - lastX;
      const dy = event.clientY - lastY;
      if (Math.hypot(dx, dy) > 3) moved = true;
      lastX = event.clientX;
      lastY = event.clientY;
      if (rotating || event.shiftKey || event.altKey) {
        angle += dx * 0.01;
      } else {
        const rect = canvas.getBoundingClientRect();
        panX += dx * (canvas.width / rect.width);
        panY += dy * (canvas.height / rect.height);
      }
      redraw();
    });

    canvas.addEventListener('pointerup', (event) => {
      canvas.releasePointerCapture(event.pointerId);
      dragging = false;
      rotating = false;
      canvas.classList.remove('rotating');
      if (moved) return;
      const node = hit(screenToWorld(event.clientX, event.clientY));
      if (!node) return;
      selectedId = node.id;
      redraw();
      if (onSelect) onSelect(node, data);
    });

    canvas.addEventListener('dblclick', () => {
      fit();
      redraw();
    });

    window.addEventListener('resize', () => {
      resize();
      redraw();
    });
  };
})();
