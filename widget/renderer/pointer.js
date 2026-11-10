// Shows the ring and its label; the main process positions the window and decides when it goes.
const el = document.getElementById('p');
const label = el.querySelector('.label');

window.pointer.onShow(({ label: text, flip }) => {
  el.classList.remove('on');
  void el.offsetWidth; // restart the landing animation for a second point in a row
  label.textContent = text || 'here';
  el.classList.toggle('flip', Boolean(flip));
  el.classList.add('on');
});

window.pointer.onHide(() => el.classList.remove('on'));
