import * as vscode from 'vscode';
import { RiskFinding, RiskReportData } from './scannerBridge';

class RiskTreeItem extends vscode.TreeItem {
  constructor(
    label: string,
    collapsibleState: vscode.TreeItemCollapsibleState,
    readonly kind: 'severity' | 'finding' | 'message',
    readonly finding?: RiskFinding
  ) {
    super(label, collapsibleState);
  }
}

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'];
const ICONS: Record<string, string> = {
  critical: 'error',
  high: 'warning',
  medium: 'circle-outline',
  low: 'info',
  info: 'info',
};

export class RiskTreeProvider implements vscode.TreeDataProvider<RiskTreeItem> {
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<RiskTreeItem | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private data: RiskReportData | undefined;
  private errorMessage: string | undefined;

  setData(data: RiskReportData): void {
    this.data = data;
    this.errorMessage = undefined;
    this._onDidChangeTreeData.fire();
  }

  setError(message: string): void {
    this.errorMessage = message;
    this.data = undefined;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: RiskTreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: RiskTreeItem): RiskTreeItem[] {
    if (this.errorMessage) {
      return [new RiskTreeItem(`$(error) ${this.errorMessage}`, vscode.TreeItemCollapsibleState.None, 'message')];
    }
    if (!this.data) {
      return [new RiskTreeItem('Run "AI Risk: Scan Workspace" to begin', vscode.TreeItemCollapsibleState.None, 'message')];
    }
    if (!element) {
      if (!this.data.findings.length) {
        return [new RiskTreeItem('No code assessment risks detected', vscode.TreeItemCollapsibleState.None, 'message')];
      }
      return SEVERITY_ORDER
        .filter((severity) => (this.data!.severity_counts[severity] || 0) > 0)
        .map((severity) => {
          const count = this.data!.severity_counts[severity] || 0;
          const item = new RiskTreeItem(
            `${severity.toUpperCase()} (${count})`,
            vscode.TreeItemCollapsibleState.Expanded,
            'severity'
          );
          item.iconPath = new vscode.ThemeIcon(ICONS[severity] || 'circle-outline');
          return item;
        });
    }

    if (element.kind === 'severity') {
      const severity = element.label?.toString().split(' ')[0].toLowerCase();
      return (this.data.findings || [])
        .filter((finding) => finding.severity === severity)
        .map((finding) => {
          const item = new RiskTreeItem(
            `${finding.title} - ${finding.file}:${finding.line}`,
            vscode.TreeItemCollapsibleState.None,
            'finding',
            finding
          );
          item.description = finding.rule_id;
          item.iconPath = new vscode.ThemeIcon(ICONS[finding.severity] || 'warning');
          item.tooltip = new vscode.MarkdownString(
            `**${finding.title}**\n\n` +
            `Severity: \`${finding.severity}\`\n\n` +
            `File: \`${finding.file}:${finding.line}\`\n\n` +
            `Suggestion: ${finding.suggestion}\n\n` +
            (finding.evidence_snippet ? `Evidence:\n\n\`\`\`\n${finding.evidence_snippet}\n\`\`\`` : '')
          );
          item.command = {
            command: 'aiStackMapper.openRiskFinding',
            title: 'Open Risk Finding',
            arguments: [this.data!.root, finding],
          };
          return item;
        });
    }

    return [];
  }
}
