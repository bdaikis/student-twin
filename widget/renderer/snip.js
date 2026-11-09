// Draws the traced line, and the shape it will close into, inside a light fill: what is inside is
// what the twin will see.
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');

function fit() {
  const r = devicePixelRatio || 1;
  canvas.width = Math.round(innerWidth * r);
  canvas.height = Math.round(innerHeight * r);
  ctx.setTransform(r, 0, 0, r, 0, 0);
}

function draw(points) {
  if (canvas.width !== Math.round(innerWidth * (devicePixelRatio || 1))) fit();
  ctx.clearRect(0, 0, innerWidth, innerHeight);
  if (points.length < 2) return;
  const trace = () => {
    ctx.beginPath();
    ctx.moveTo(points[0][0], points[0][1]);
    for (const [x, y] of points.slice(1)) ctx.lineTo(x, y);
  };
  trace();
  ctx.closePath();
  ctx.fillStyle = 'rgba(90, 160, 255, 0.12)';
  ctx.fill();
  // The closing line back to the start, dashed: it is where the shape will close on release.
  const [sx, sy] = points[0];
  const [ex, ey] = points[points.length - 1];
  ctx.setLineDash([5, 4]);
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)';
  ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(sx, sy); ctx.stroke();
  ctx.setLineDash([]);
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  // White line on a dark edge, readable on light and dark pages.
  trace();
  ctx.lineWidth = 4;
  ctx.strokeStyle = 'rgba(0, 0, 0, 0.45)';
  ctx.stroke();
  ctx.lineWidth = 2;
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.95)';
  ctx.stroke();
}

addEventListener('resize', fit);
fit();
window.snip.onPath(draw);
