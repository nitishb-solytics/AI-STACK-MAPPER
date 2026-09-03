import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs/promises';
import { ScannerBridge, Occurrence, LLM_API_KEY_SECRET, RiskFinding } from './scannerBridge';
import { AiStackTreeProvider } from './treeViewProvider';
import { RiskTreeProvider } from './riskTreeViewProvider';

let statusBarItem: vscode.StatusBarItem;
let debounceTimer: ReturnType<typeof setTimeout> | undefined;

const VAULT_TOKEN_SECRET = 'aiStackMapper.vaultToken';

function getWorkspaceRoot(): string | undefined {
  const folders = vscode.workspace.workspaceFolders;
  return folders?.[0]?.uri.fsPath;
}

async function readJsonFile<T>(filePath: string): Promise<T | undefined> {
  try {
    const raw = await fs.readFile(filePath, 'utf8');
    return JSON.parse(raw) as T;
  } catch {
    return undefined;
  }
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

function normalizeJwtHeader(token: string): string {
  const trimmed = token.trim();
  if (/^(JWT|Bearer)\s+/i.test(trimmed)) {
    return trimmed;
  }
  return `JWT ${trimmed}`;
}

export function activate(context: vscode.ExtensionContext): void {
  const treeProvider = new AiStackTreeProvider();
  const riskTreeProvider = new RiskTreeProvider();
  const bridge = new ScannerBridge(context.extensionPath, context.secrets);

  context.subscriptions.push(vscode.window.registerTreeDataProvider('aiStackMapperView', treeProvider));
  context.subscriptions.push(vscode.window.registerTreeDataProvider('aiStackMapperRiskView', riskTreeProvider));

  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = 'aiStackMapper.scan';
  context.subscriptions.push(statusBarItem);

  async function runScan(): Promise<void> {
    const folders = vscode.workspace.workspaceFolders;
    if (!folders || folders.length === 0) {
      vscode.window.showWarningMessage('AI Stack Mapper: open a folder or workspace first.');
      return;
    }
    const root = folders[0].uri.fsPath;
    statusBarItem.text = '$(sync~spin) AI Stack: scanning...';
    statusBarItem.show();
    try {
      const result = await bridge.scan(root);
      const data = result.data;
      treeProvider.setData(data);
      statusBarItem.text = `$(circuit-board) AI Stack: ${data.total_components}`;
      statusBarItem.tooltip = `Generated ${path.basename(result.markdownPath)} and ${path.basename(result.jsonPath)}. Click to re-scan.`;
      const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(result.markdownPath));
      await vscode.window.showTextDocument(doc);
      vscode.window.showInformationMessage(
        `AI Stack Mapper: generated ${path.basename(result.markdownPath)} and ${path.basename(result.jsonPath)}.`
      );
    } catch (err: any) {
      treeProvider.setError(err.message);
      statusBarItem.text = '$(error) AI Stack: scan failed';
      statusBarItem.tooltip = err.message;
      vscode.window.showErrorMessage(`AI Stack Mapper: ${err.message}`);
    }
  }

  context.subscriptions.push(
    vscode.commands.registerCommand('aiStackMapper.scan', runScan),

    vscode.commands.registerCommand('aiStackMapper.scanRisks', async () => {
      const folders = vscode.workspace.workspaceFolders;
      if (!folders || folders.length === 0) {
        vscode.window.showWarningMessage('AI Risk Scanner: open a folder or workspace first.');
        return;
      }
      const root = folders[0].uri.fsPath;
      statusBarItem.text = '$(sync~spin) AI Risk: scanning...';
      statusBarItem.show();
      try {
        const result = await bridge.scanRisks(root);
        riskTreeProvider.setData(result.data);
        statusBarItem.text = '$(shield) AI Risk: report ready';
        statusBarItem.tooltip = `Generated ${path.basename(result.markdownPath)} and ${path.basename(result.jsonPath)}.`;
        const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(result.markdownPath));
        await vscode.window.showTextDocument(doc);
        vscode.window.showInformationMessage(
          `AI Risk Scanner: generated ${path.basename(result.markdownPath)} and ${path.basename(result.jsonPath)}.`
        );
      } catch (err: any) {
        riskTreeProvider.setError(err.message);
        statusBarItem.text = '$(error) AI Risk: scan failed';
        statusBarItem.tooltip = err.message;
        vscode.window.showErrorMessage(`AI Risk Scanner: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand('aiStackMapper.publishToVault', async () => {
      const root = getWorkspaceRoot();
      if (!root) {
        vscode.window.showWarningMessage('AI Stack Mapper: open the client repository folder first.');
        return;
      }

      const cfg = vscode.workspace.getConfiguration('aiStackMapper');
      const defaultBaseUrl = cfg.get<string>('vaultBaseUrl', 'http://127.0.0.1:8000') || 'http://127.0.0.1:8000';
      const baseUrl = await vscode.window.showInputBox({
        prompt: 'Vault backend base URL',
        value: defaultBaseUrl,
        ignoreFocusOut: true,
      });
      if (!baseUrl) return;

      const defaultOrg = cfg.get<string>('vaultOrg', '') || '';
      const org = await vscode.window.showInputBox({
        prompt: 'Vault Org header / tenant account id',
        value: defaultOrg,
        placeHolder: 'vault_agent365',
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

      const mode = await vscode.window.showQuickPick(
        [
          { label: 'Import to Vault', description: 'Create AIAgent and linked AIComponents', discover: true },
          { label: 'Preview only', description: 'Validate mapping without creating Vault records', discover: false },
        ],
        { placeHolder: 'Choose how to publish scanner results to Vault', ignoreFocusOut: true }
      );
      if (!mode) return;

      statusBarItem.text = '$(sync~spin) AI Stack: preparing Vault publish...';
      statusBarItem.show();

      try {
        const stackPath = path.join(root, 'ai-stack-report.json');
        const riskPath = path.join(root, 'ai-risk-report.json');

        let stack = await readJsonFile<any>(stackPath);
        if (!stack) {
          statusBarItem.text = '$(sync~spin) AI Stack: scanning before publish...';
          const stackResult = await bridge.scan(root);
          stack = stackResult.data;
          treeProvider.setData(stack);
        }

        let risk = await readJsonFile<any>(riskPath);
        if (!risk) {
          statusBarItem.text = '$(sync~spin) AI Risk: scanning before publish...';
          const riskResult = await bridge.scanRisks(root);
          risk = riskResult.data;
          riskTreeProvider.setData(risk);
        }

        const endpoint = `${baseUrl.replace(/\/+$/, '')}/vault/ai-governance/codebase-discovery`;
        const payload = {
          source_type: 'scanner_report',
          repo_name: path.basename(root),
          repo_path: root,
          repo_url: repoUrl || '',
          branch: branch || '',
          stack,
          risk,
          discover: mode.discover,
          include_risks: true,
        };

        statusBarItem.text = '$(cloud-upload) AI Stack: publishing to Vault...';
        const result = await postJson(
          endpoint,
          {
            Authorization: normalizeJwtHeader(token),
            Org: org,
          },
          payload
        );

        const summary = result?.summary
          ? `agents=${result.summary.agents}, components=${result.summary.components}, linked=${result.summary.linked}`
          : `agents=${result?.agents?.length ?? 0}`;
        statusBarItem.text = mode.discover ? '$(check) AI Stack: published to Vault' : '$(check) AI Stack: Vault preview ready';
        statusBarItem.tooltip = `Vault ${mode.discover ? 'import' : 'preview'} completed: ${summary}`;
        vscode.window.showInformationMessage(
          `AI Stack Mapper: Vault ${mode.discover ? 'import' : 'preview'} completed (${summary}).`
        );
      } catch (err: any) {
        statusBarItem.text = '$(error) AI Stack: Vault publish failed';
        statusBarItem.tooltip = err.message;
        vscode.window.showErrorMessage(`AI Stack Mapper Vault publish failed: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand('aiStackMapper.openRiskFinding', async (root: string, finding: RiskFinding) => {
      try {
        const uri = vscode.Uri.file(path.join(root, finding.file));
        const doc = await vscode.workspace.openTextDocument(uri);
        const editor = await vscode.window.showTextDocument(doc);
        const line = Math.max(0, Math.min(finding.line - 1, doc.lineCount - 1));
        const range = doc.lineAt(line).range;
        editor.selection = new vscode.Selection(range.start, range.start);
        editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
      } catch {
        vscode.window.showWarningMessage(`AI Risk Scanner: could not open ${finding.file}:${finding.line}`);
      }
    }),

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
    }),

    vscode.commands.registerCommand('aiStackMapper.setLlmApiKey', async () => {
      const key = await vscode.window.showInputBox({
        prompt: 'Enter your LLM API key (e.g. OpenAI, Azure OpenAI, or a self-hosted endpoint\'s key).',
        placeHolder: 'sk-...',
        password: true,
        ignoreFocusOut: true,
      });
      if (!key) {
        return;
      }
      await context.secrets.store(LLM_API_KEY_SECRET, key);
      vscode.window.showInformationMessage(
        'AI Stack Mapper: LLM API key saved securely. Enable "aiStackMapper.enrichWithLLM" for stack enrichment or "aiStackMapper.riskUseLLM" for risk controls, then re-run a scan.'
      );
    }),

    vscode.commands.registerCommand('aiStackMapper.clearLlmApiKey', async () => {
      await context.secrets.delete(LLM_API_KEY_SECRET);
      vscode.window.showInformationMessage('AI Stack Mapper: LLM API key cleared.');
    })
  );

  const saveWatcher = vscode.workspace.onDidSaveTextDocument((doc) => {
    const cfg = vscode.workspace.getConfiguration('aiStackMapper');
    if (!cfg.get<boolean>('scanOnSave', true)) return;
    const relevant = /\.(py|json|toml|txt|env)$/.test(doc.fileName) || path.basename(doc.fileName).startsWith('.env');
    if (!relevant) return;
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runScan, 1500);
  });
  context.subscriptions.push(saveWatcher);

  // Initial scan when the extension activates.
  // void runScan();
}

export function deactivate(): void {
  if (debounceTimer) clearTimeout(debounceTimer);
}
