import * as vscode from 'vscode';
import * as path from 'path';
import { ScannerBridge, Occurrence } from './scannerBridge';
import { AiStackTreeProvider } from './treeViewProvider';

let statusBarItem: vscode.StatusBarItem;
let debounceTimer: ReturnType<typeof setTimeout> | undefined;

const VAULT_TOKEN_SECRET = 'aiStackMapper.vaultToken';

function getWorkspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function normalizeJwtHeader(token: string): string {
  const trimmed = token.trim();
  if (/^(JWT|Bearer)\s+/i.test(trimmed)) {
    return trimmed;
  }
  return `JWT ${trimmed}`;
}

async function postJson(url: string, headers: Record<string, string>, body: unknown): Promise<any> {
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...headers,
    },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  let data: any = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }
  if (!response.ok) {
    const msg = data?.msg || data?.detail || data?.message || text || response.statusText;
    throw new Error(`Vault returned HTTP ${response.status}: ${msg}`);
  }
  return data;
}

export function activate(context: vscode.ExtensionContext): void {
  const treeProvider = new AiStackTreeProvider();
  const bridge = new ScannerBridge(context.extensionPath, context.secrets);

  context.subscriptions.push(vscode.window.registerTreeDataProvider('aiStackMapperView', treeProvider));

  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = 'aiStackMapper.scan';
  context.subscriptions.push(statusBarItem);

  async function runScan(): Promise<void> {
    const root = getWorkspaceRoot();
    if (!root) {
      vscode.window.showWarningMessage('AI Stack Mapper: open a folder or workspace first.');
      return;
    }

    statusBarItem.text = '$(sync~spin) AI Stack: scanning...';
    statusBarItem.show();
    try {
      const result = await bridge.scan(root);
      const data = result.data;
      treeProvider.setData(data);
      statusBarItem.text = `$(circuit-board) AI Stack: ${data.local_agents?.length || 0} local agent(s)`;
      statusBarItem.tooltip = `Generated ${path.basename(result.markdownPath)} and ${path.basename(result.jsonPath)}.`;
      const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(result.markdownPath));
      await vscode.window.showTextDocument(doc);
      vscode.window.showInformationMessage(
        `AI Stack Mapper: found ${data.local_agents?.length || 0} local agent(s), ${data.total_components} component(s).`
      );
    } catch (err: any) {
      treeProvider.setError(err.message);
      statusBarItem.text = '$(error) AI Stack: scan failed';
      statusBarItem.tooltip = err.message;
      vscode.window.showErrorMessage(`AI Stack Mapper: ${err.message}`);
    }
  }

  async function publishToVault(): Promise<void> {
    const root = getWorkspaceRoot();
    if (!root) {
      vscode.window.showWarningMessage('AI Stack Mapper: open the repository folder to scan and publish.');
      return;
    }

    const cfg = vscode.workspace.getConfiguration('aiStackMapper');
    const baseUrl = await vscode.window.showInputBox({
      prompt: 'Vault backend base URL',
      value: cfg.get<string>('vaultBaseUrl', 'http://127.0.0.1:8000') || 'http://127.0.0.1:8000',
      ignoreFocusOut: true,
    });
    if (!baseUrl) return;

    const org = await vscode.window.showInputBox({
      prompt: 'Vault Org header / tenant account id',
      value: cfg.get<string>('vaultOrg', '') || '',
      placeHolder: 'ai-gov-3',
      ignoreFocusOut: true,
    });
    if (!org) return;

    const storedToken = await context.secrets.get(VAULT_TOKEN_SECRET);
    const token = await vscode.window.showInputBox({
      prompt: 'Vault JWT token. Paste token only, or full "JWT <token>" value.',
      value: storedToken || '',
      password: true,
      ignoreFocusOut: true,
    });
    if (!token) return;
    await context.secrets.store(VAULT_TOKEN_SECRET, token);

    const repoUrl = await vscode.window.showInputBox({
      prompt: 'Optional Git repository URL to store as metadata',
      value: cfg.get<string>('vaultRepoUrl', '') || '',
      ignoreFocusOut: true,
    });
    const branch = await vscode.window.showInputBox({
      prompt: 'Optional Git branch to store as metadata',
      value: cfg.get<string>('vaultBranch', '') || '',
      ignoreFocusOut: true,
    });

    statusBarItem.text = '$(sync~spin) AI Stack: scanning before Vault publish...';
    statusBarItem.show();

    try {
      const stackResult = await bridge.scan(root);
      const stack = stackResult.data;
      treeProvider.setData(stack);

      const endpoint = `${baseUrl.replace(/\/+$/, '')}/vault/ai-governance/codebase-discovery`;
      const payload = await bridge.buildVaultPayload(root, stackResult.jsonPath, {
        repoUrl: repoUrl || '',
        branch: branch || '',
        selectedKeys: (stack.local_agents || []).map((agent) => agent.key),
      });

      statusBarItem.text = '$(cloud-upload) AI Stack: publishing local discovery...';
      const result = await postJson(
        endpoint,
        {
          Authorization: normalizeJwtHeader(token),
          Org: org,
        },
        payload
      );

      const counts = result?.agent_counts || {};
      const total = counts.total ?? payload.local_agents.length;
      statusBarItem.text = '$(check) AI Stack: discovery published';
      statusBarItem.tooltip = `Published ${total} local agent(s) to Vault Auto Discovery staging.`;
      vscode.window.showInformationMessage(
        `AI Stack Mapper: published ${total} local agent(s) to Vault Auto Discovery. Open Vault → AIAgent → Auto Discovery → Agent Discovery in Local to import.`
      );
    } catch (err: any) {
      statusBarItem.text = '$(error) AI Stack: Vault publish failed';
      statusBarItem.tooltip = err.message;
      vscode.window.showErrorMessage(`AI Stack Mapper Vault publish failed: ${err.message}`);
    }
  }

  context.subscriptions.push(
    vscode.commands.registerCommand('aiStackMapper.scan', runScan),
    vscode.commands.registerCommand('aiStackMapper.publishToVault', publishToVault),
    vscode.commands.registerCommand('aiStackMapper.openOccurrence', async (root: string, occ: Occurrence) => {
      try {
        const uri = vscode.Uri.file(path.join(root, occ.file));
        const doc = await vscode.workspace.openTextDocument(uri);
        const editor = await vscode.window.showTextDocument(doc);
        const line = Math.max(0, Math.min(occ.line - 1, doc.lineCount - 1));
        const range = doc.lineAt(line).range;
        editor.selection = new vscode.Selection(range.start, range.start);
        editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
      } catch {
        vscode.window.showWarningMessage(`AI Stack Mapper: could not open ${occ.file}:${occ.line}`);
      }
    }),
    vscode.commands.registerCommand('aiStackMapper.exportMarkdown', async () => {
      const data = treeProvider.getScanData();
      if (!data) {
        vscode.window.showWarningMessage('AI Stack Mapper: run a scan first.');
        return;
      }
      const lines: string[] = ['# AI Stack Report', ''];
      for (const [cat, comps] of Object.entries(data.categories)) {
        if (comps.length === 0) continue;
        lines.push(`## ${cat}`, '');
        for (const c of comps) {
          lines.push(`- **${c.name}** (${c.confidence}, ${c.count} occurrence(s))`);
        }
        lines.push('');
      }
      const doc = await vscode.workspace.openTextDocument({ content: lines.join('\n'), language: 'markdown' });
      await vscode.window.showTextDocument(doc);
    })
  );

  const saveWatcher = vscode.workspace.onDidSaveTextDocument((doc) => {
    const cfg = vscode.workspace.getConfiguration('aiStackMapper');
    if (!cfg.get<boolean>('scanOnSave', false)) return;
    const relevant = /\.(py|json|toml|txt|env)$/.test(doc.fileName) || path.basename(doc.fileName).startsWith('.env');
    if (!relevant) return;
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runScan, 1500);
  });
  context.subscriptions.push(saveWatcher);
}

export function deactivate(): void {
  if (debounceTimer) clearTimeout(debounceTimer);
}
