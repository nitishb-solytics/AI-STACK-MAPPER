import * as vscode from 'vscode';
import * as path from 'path';
import { ScannerBridge, Occurrence, LLM_API_KEY_SECRET, RiskFinding } from './scannerBridge';
import { AiStackTreeProvider } from './treeViewProvider';
import { RiskTreeProvider } from './riskTreeViewProvider';

let statusBarItem: vscode.StatusBarItem;
let debounceTimer: ReturnType<typeof setTimeout> | undefined;

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
      const data = await bridge.scan(root);
      treeProvider.setData(data);
      statusBarItem.text = `$(circuit-board) AI Stack: ${data.total_components}`;
      statusBarItem.tooltip = `${data.total_components} AI-stack components across ${data.scanned_files} files. Click to re-scan.`;
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
  void runScan();
}

export function deactivate(): void {
  if (debounceTimer) clearTimeout(debounceTimer);
}
