import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as path from 'path';
import { promisify } from 'util';
import { ScannerBridge, Occurrence, RiskFinding } from './scannerBridge';
import { AiStackTreeProvider } from './treeViewProvider';
import { RiskTreeProvider } from './riskTreeViewProvider';
import { askVaultDetails } from './publishForm';
import { signInWithBrowser, VaultSession } from './vaultSignIn';

const execFile = promisify(cp.execFile);

let statusBarItem: vscode.StatusBarItem;
let debounceTimer: ReturnType<typeof setTimeout> | undefined;

// Pasted tokens were stored here before browser sign-in replaced them.
const LEGACY_VAULT_TOKEN_SECRET = 'aiStackMapper.vaultToken';
// Last values actually used for a publish. The Vault instance (base URL)
// is usually the same across repos so it lives in globalState; the repo URL and
// branch are per-repo and live in workspaceState.
const LAST_BASE_URL = 'aiStackMapper.lastBaseUrl';
const LAST_REPO_URL = 'aiStackMapper.lastRepoUrl';
const LAST_BRANCH = 'aiStackMapper.lastBranch';

function getWorkspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

/**
 * Value the user explicitly configured for `key`, ignoring the default declared
 * in package.json. `WorkspaceConfiguration.get` cannot distinguish "the user
 * chose this" from "this is the shipped default", and that difference decides
 * whether a configured setting should win over the last value actually used.
 */
function explicitSetting(cfg: vscode.WorkspaceConfiguration, key: string): string {
  const info = cfg.inspect<string>(key);
  return (info?.workspaceFolderValue ?? info?.workspaceValue ?? info?.globalValue ?? '').trim();
}

async function gitValue(root: string, args: string[]): Promise<string> {
  try {
    const { stdout } = await execFile('git', ['-C', root, ...args], { timeout: 5000 });
    return stdout.trim();
  } catch {
    return '';
  }
}

/** Strip `//user:password@` credentials; leaves `git@host:path` SSH URLs alone. */
function stripUrlCredentials(url: string): string {
  return url.replace(/^([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)[^/@]*:[^/@]*@/, '$1');
}

/**
 * Origin remote of the scanned repo. This is the stable input to the Vault
 * `external_id`, so deriving it from git rather than asking the user to retype
 * it each publish is what stops one repo staging duplicate agents.
 */
async function detectRepoUrl(root: string): Promise<string> {
  const url =
    (await gitValue(root, ['remote', 'get-url', 'origin'])) ||
    // `remote get-url` needs git >= 2.7; fall back for older clients.
    (await gitValue(root, ['config', '--get', 'remote.origin.url']));
  return stripUrlCredentials(url);
}

async function detectBranch(root: string): Promise<string> {
  const branch = await gitValue(root, ['rev-parse', '--abbrev-ref', 'HEAD']);
  return branch === 'HEAD' ? '' : branch; // detached HEAD has no branch name
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
  const riskTreeProvider = new RiskTreeProvider();
  const bridge = new ScannerBridge(context.extensionPath);
  void context.secrets.delete(LEGACY_VAULT_TOKEN_SECRET);

  context.subscriptions.push(vscode.window.registerTreeDataProvider('aiStackMapperView', treeProvider));
  context.subscriptions.push(vscode.window.registerTreeDataProvider('aiStackMapperRiskView', riskTreeProvider));

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

    // Repo URL and branch are resolved, never typed. The URL is the input to
    // every agent's Vault `external_id`, so a free-text box makes identity a
    // function of what someone happened to paste: the same repository entered
    // as `.git`, without it, or in a different case stages a second copy of
    // every agent. Resolving from the checkout removes that entire class of
    // mistake. `vaultRepoUrl`/`vaultBranch` remain as deliberate pins for the
    // rare case the remote is not the right identity; the remembered values
    // are only a fallback for when git is unavailable.
    const [detectedUrl, detectedBranch] = await Promise.all([detectRepoUrl(root), detectBranch(root)]);
    const pinnedUrl = explicitSetting(cfg, 'vaultRepoUrl');
    const pinnedBranch = explicitSetting(cfg, 'vaultBranch');
    const repoUrl = pinnedUrl || detectedUrl || context.workspaceState.get<string>(LAST_REPO_URL) || '';
    const branch = pinnedBranch || detectedBranch || context.workspaceState.get<string>(LAST_BRANCH) || '';

    const originLabel = pinnedUrl
      ? 'pinned by the aiStackMapper.vaultRepoUrl setting'
      : detectedUrl
        ? 'from this checkout’s git origin'
        : 'remembered from the last publish';
    const identityNote = repoUrl
      ? `Identity is ${originLabel}.`
      : 'No git remote was found and no repository URL is pinned. Agent identity will fall back to ' +
        'this machine’s local path, so publishing the same repository from another checkout will ' +
        'stage a duplicate set of agents in Vault. Set aiStackMapper.vaultRepoUrl, or add a git ' +
        'remote, to give this repository a stable identity.';

    const details = await askVaultDetails(context.extensionPath, {
      baseUrl:
        explicitSetting(cfg, 'vaultBaseUrl') ||
        context.globalState.get<string>(LAST_BASE_URL) ||
        cfg.get<string>('vaultBaseUrl', ''),
      repoUrl,
      branch,
      identityNote,
    });
    if (!details) return;
    const { baseUrl } = details;

    // Remembered now rather than after a successful POST: the common failure is
    // "Vault is not up yet", and re-entering every field to retry is the
    // friction this exists to remove.
    await context.globalState.update(LAST_BASE_URL, baseUrl);
    await context.workspaceState.update(LAST_REPO_URL, repoUrl);
    await context.workspaceState.update(LAST_BRANCH, branch);

    // Tenant and token come from the user's own login, never typed or stored.
    let session: VaultSession;
    try {
      session = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: 'AI Stack Mapper: sign in to Vault in your browser…',
          cancellable: true,
        },
        (_progress, cancel) => signInWithBrowser(cfg.get<string>('vaultLoginUrl', ''), cancel)
      );
    } catch (err: any) {
      vscode.window.showErrorMessage(`AI Stack Mapper: Vault ${err.message}`);
      return;
    }

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
          Authorization: session.authorization,
          Org: session.org,
        },
        payload
      );

      const counts = result?.agent_counts || {};
      const total = counts.total ?? payload.local_agents.length;
      statusBarItem.text = '$(check) AI Stack: discovery published';
      statusBarItem.tooltip = `Published ${total} local agent(s) to Vault Auto Discovery staging.`;
      vscode.window.showInformationMessage(
        `AI Stack Mapper: published ${total} local agent(s) to Vault Auto Discovery on tenant ${session.org}. Open Vault → AIAgent → Auto Discovery → Agent Discovery in Local to import.`
      );
    } catch (err: any) {
      statusBarItem.text = '$(error) AI Stack: Vault publish failed';
      statusBarItem.tooltip = err.message;
      vscode.window.showErrorMessage(`AI Stack Mapper Vault publish failed: ${err.message}`);
    }
  }

  context.subscriptions.push(
    vscode.commands.registerCommand('aiStackMapper.scan', runScan),
    vscode.commands.registerCommand('aiStackMapper.scanRisks', async () => {
      const root = getWorkspaceRoot();
      if (!root) {
        vscode.window.showWarningMessage('AI Risk Scanner: open a folder or workspace first.');
        return;
      }
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
          `AI Risk Scanner: ${result.data.status}, ${result.data.findings.length} finding(s). Risk data was not published to Vault.`
        );
      } catch (err: any) {
        riskTreeProvider.setError(err.message);
        statusBarItem.text = '$(error) AI Risk: scan failed';
        statusBarItem.tooltip = err.message;
        vscode.window.showErrorMessage(`AI Risk Scanner: ${err.message}`);
      }
    }),
    vscode.commands.registerCommand('aiStackMapper.publishToVault', publishToVault),
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
