import * as vscode from 'vscode';
import * as crypto from 'crypto';

export interface VaultDetails {
  baseUrl: string;
  org: string;
  /** Empty when the user kept the saved token. */
  token: string;
}

export interface PublishFormDefaults {
  baseUrl: string;
  org: string;
  hasSavedToken: boolean;
  repoUrl: string;
  branch: string;
  identityNote: string;
}

/**
 * One form for every publish input, instead of a chain of input boxes in the
 * command palette. Resolves undefined when the user cancels or closes the tab.
 * The saved token is never sent into the webview; a blank field means "reuse it".
 */
export function askVaultDetails(defaults: PublishFormDefaults): Promise<VaultDetails | undefined> {
  const panel = vscode.window.createWebviewPanel(
    'aiStackMapperPublish',
    'Publish to Vault',
    vscode.ViewColumn.Active,
    { enableScripts: true }
  );
  panel.webview.html = formHtml(crypto.randomBytes(16).toString('base64'), defaults);
  return new Promise((resolve) => {
    panel.webview.onDidReceiveMessage((msg: VaultDetails | null) => {
      resolve(msg ?? undefined);
      panel.dispose();
    });
    panel.onDidDispose(() => resolve(undefined));
  });
}

function formHtml(nonce: string, defaults: PublishFormDefaults): string {
  // `<` escaped so a value can never close the script tag it is embedded in.
  const data = JSON.stringify(defaults).replace(/</g, '\\u003c');
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<style nonce="${nonce}">
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 16px 24px; max-width: 560px; }
  label { display: block; margin: 14px 0 4px; font-weight: 600; }
  input { width: 100%; box-sizing: border-box; padding: 6px 8px; color: var(--vscode-input-foreground);
          background: var(--vscode-input-background); border: 1px solid var(--vscode-input-border, transparent); }
  input:focus { outline: 1px solid var(--vscode-focusBorder); }
  .hint { color: var(--vscode-descriptionForeground); font-size: 0.9em; margin: 4px 0 0; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 20px 0 4px; }
  dt { color: var(--vscode-descriptionForeground); }
  dd { margin: 0; word-break: break-all; }
  .warn { color: var(--vscode-editorWarning-foreground); }
  .actions { margin-top: 20px; display: flex; gap: 8px; }
  button { padding: 6px 14px; border: none; cursor: pointer; color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
  button:hover { background: var(--vscode-button-hoverBackground); }
  button.secondary { color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); }
</style>
</head>
<body>
<h2>Publish local agent discovery to Vault</h2>
<form id="form">
  <label for="baseUrl">Vault backend base URL</label>
  <input id="baseUrl" name="baseUrl" type="url" required placeholder="https://your-vault-backend.example.com">
  <label for="org">Org (tenant slug)</label>
  <input id="org" name="org" required placeholder="vault_agent365">
  <label for="token">Access token</label>
  <input id="token" name="token" type="password" autocomplete="off">
  <p class="hint">Paste the full Authorization header value: <code>Bearer &lt;token&gt;</code> or <code>JWT &lt;token&gt;</code>. A bare token is sent as Bearer.</p>
  <dl>
    <dt>Repository</dt><dd id="repo"></dd>
    <dt>Branch</dt><dd id="branch"></dd>
  </dl>
  <p id="note" class="hint"></p>
  <div class="actions">
    <button type="submit">Publish</button>
    <button type="button" id="cancel" class="secondary">Cancel</button>
  </div>
</form>
<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  const d = ${data};
  const form = document.getElementById('form');
  form.baseUrl.value = d.baseUrl;
  form.org.value = d.org;
  form.token.required = !d.hasSavedToken;
  form.token.placeholder = d.hasSavedToken ? 'Leave blank to reuse the saved token' : 'Bearer <token>';
  document.getElementById('repo').textContent = d.repoUrl || '(none)';
  document.getElementById('branch').textContent = d.branch || '(none)';
  const note = document.getElementById('note');
  note.textContent = d.identityNote;
  if (!d.repoUrl) note.className = 'warn';
  (d.baseUrl && d.org ? form.token : form.baseUrl).focus();
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    vscode.postMessage({ baseUrl: form.baseUrl.value.trim(), org: form.org.value.trim(), token: form.token.value.trim() });
  });
  document.getElementById('cancel').addEventListener('click', () => vscode.postMessage(null));
</script>
</body>
</html>`;
}
