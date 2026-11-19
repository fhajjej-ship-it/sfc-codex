const button = document.querySelector('#copy-install');
const command = document.querySelector('#install-command');
const status = document.querySelector('#copy-status');

button.hidden = false;
button.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(command.textContent);
    status.textContent = 'Command copied.';
  } catch {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(command);
    selection.removeAllRanges();
    selection.addRange(range);
    status.textContent = 'Command selected. Press Command+C or Ctrl+C to copy.';
  }
});
