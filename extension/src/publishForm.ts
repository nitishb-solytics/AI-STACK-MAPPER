import * as vscode from 'vscode';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';

export interface VaultDetails {
  baseUrl: string;
}

export interface PublishFormDefaults {
  baseUrl: string;
  repoUrl: string;
  branch: string;
  identityNote: string;
}

/**
 * One form for the publish inputs, instead of a chain of input boxes in the
 * command palette. Resolves undefined when the user cancels or closes the tab.
 * Tenant and token are not asked for: they come from the browser sign-in.
 */
export function askVaultDetails(extensionPath: string, defaults: PublishFormDefaults): Promise<VaultDetails | undefined> {
  const panel = vscode.window.createWebviewPanel(
    'aiStackMapperPublish',
    'Publish to Vault',
    vscode.ViewColumn.Active,
    { enableScripts: true }
  );
  // Inlined rather than loaded as an <img> so CSS can recolour the wordmark per theme.
  const logo = fs.readFileSync(path.join(extensionPath, 'resources', 'solytics-logo.svg'), 'utf8');
  panel.webview.html = formHtml(crypto.randomBytes(16).toString('base64'), logo, defaults);
  return new Promise((resolve) => {
    panel.webview.onDidReceiveMessage((msg: VaultDetails | null) => {
      resolve(msg ?? undefined);
      panel.dispose();
    });
    panel.onDidDispose(() => resolve(undefined));
  });
}

function formHtml(nonce: string, logo: string, defaults: PublishFormDefaults): string {
  // `<` escaped so a value can never close the script tag it is embedded in.
  const data = JSON.stringify(defaults).replace(/</g, '\\u003c');
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<style nonce="${nonce}">
  /* Solytics Partners brand: navy wordmark, teal bulb. */
  :root { --navy: #274796; --navy-hover: #1e3876; --teal: #1F8E9F; --teal-text: #17707e; }
  body.vscode-dark, body.vscode-high-contrast:not(.vscode-high-contrast-light) { --teal-text: #4fb8c7; }
  /* The navy wordmark is unreadable on a dark editor background. */
  body.vscode-dark .logo [fill="#274796"],
  body.vscode-high-contrast:not(.vscode-high-contrast-light) .logo [fill="#274796"] { fill: #ffffff; }

  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 24px; }
  main { max-width: 600px; margin: 0 auto; }
  header { padding-bottom: 16px; margin-bottom: 20px; border-bottom: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.3)); }
  .logo svg { display: block; height: 30px; width: auto; }
  h1 { font-size: 1.35em; font-weight: 600; margin: 16px 0 4px; }
  .subtitle { color: var(--vscode-descriptionForeground); margin: 0; }
  form { padding: 4px 20px 20px; border: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.3));
         border-top: 3px solid var(--teal); border-radius: 8px; background: var(--vscode-editorWidget-background); }
  label { display: block; margin: 16px 0 6px; font-weight: 600; }
  input { width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 4px; font: inherit;
          color: var(--vscode-input-foreground); background: var(--vscode-input-background);
          border: 1px solid var(--vscode-input-border, rgba(128, 128, 128, 0.35)); }
  input:focus { outline: none; border-color: var(--teal); box-shadow: 0 0 0 1px var(--teal); }
  .hint { color: var(--vscode-descriptionForeground); font-size: 0.9em; margin: 6px 0 0; }
  code { color: var(--teal-text); font-family: var(--vscode-editor-font-family); }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 14px; margin: 20px 0 0;
       padding: 10px 12px; border-left: 3px solid var(--teal); border-radius: 4px; background: var(--vscode-textBlockQuote-background); }
  dt { color: var(--vscode-descriptionForeground); }
  dd { margin: 0; word-break: break-all; }
  .warn { color: var(--vscode-editorWarning-foreground); }
  .signin { margin: 20px 0 0; padding: 10px 12px; border-radius: 4px; background: rgba(31, 142, 159, 0.12); }
  .actions { margin-top: 20px; display: flex; gap: 10px; }
  button { padding: 8px 18px; border-radius: 4px; font: inherit; font-weight: 600; cursor: pointer;
           color: #ffffff; background: var(--navy); border: 1px solid var(--navy); }
  button:hover { background: var(--navy-hover); border-color: var(--navy-hover); }
  button:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
  button.secondary { color: var(--vscode-foreground); background: transparent; border-color: var(--vscode-widget-border, rgba(128, 128, 128, 0.5)); }
  button.secondary:hover { background: var(--vscode-toolbar-hoverBackground); }
</style>
</head>
<body>
<main>
<header>
  <div class="logo" role="img" aria-label="Solytics Partners">${logo}</div>
  <h1>Publish local agent discovery to Vault</h1>
  <p class="subtitle">Stages the agents found in this workspace for import in Vault AI Governance.</p>
</header>
<form id="form">
  <label for="baseUrl">Vault backend base URL</label>
  <input id="baseUrl" name="baseUrl" type="url" required placeholder="https://your-vault-backend.example.com">
  <p class="hint">The Vault API host for the environment you sign in to. Remembered for next time.</p>
  <dl>
    <dt>Repository</dt><dd id="repo"></dd>
    <dt>Branch</dt><dd id="branch"></dd>
  </dl>
  <p id="note" class="hint"></p>
  <p class="signin">Your browser opens the Solytics login. Sign in and pick the tenant; the scan is published to that tenant with your login, so no token or Org is typed here.</p>
  <div class="actions">
    <button type="submit">Sign in &amp; publish</button>
    <button type="button" id="cancel" class="secondary">Cancel</button>
  </div>
</form>
</main>
<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  const d = ${data};
  const form = document.getElementById('form');
  form.baseUrl.value = d.baseUrl;
  document.getElementById('repo').textContent = d.repoUrl || '(none)';
  document.getElementById('branch').textContent = d.branch || '(none)';
  const note = document.getElementById('note');
  note.textContent = d.identityNote;
  if (!d.repoUrl) note.className = 'warn';
  (d.baseUrl ? form.querySelector('button[type=submit]') : form.baseUrl).focus();
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    vscode.postMessage({ baseUrl: form.baseUrl.value.trim() });
  });
  document.getElementById('cancel').addEventListener('click', () => vscode.postMessage(null));
</script>
</body>
</html>`;
}
