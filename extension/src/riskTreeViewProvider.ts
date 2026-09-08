import * as vscode from 'vscode';
import { RiskFinding, RiskReportData } from './scannerBridge';

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'];

const SEVERITY_ICONS: { [severity: string]: string } = {
  critical: 'error',
  high: 'warning',
  medium: 'issues',
  low: 'info',
  info: 'circle-outline',
};

type RiskNodeKind = 'message' | 'severity' | 'area' | 'finding';

export class RiskTreeItem extends vscode.TreeItem {
  constructor(
    label: string,
    collapsibleState: vscode.TreeItemCollapsibleState,
    public readonly kind: RiskNodeKind,
    public readonly severity?: string,
    public readonly area?: string,
    public readonly finding?: RiskFinding
  ) {
    super(label, collapsibleState);
  }
}

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

  clear(): void {
    this.data = undefined;
    this.errorMessage = undefined;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: RiskTreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: RiskTreeItem): RiskTreeItem[] {
    if (this.errorMessage) {
      return [this.messageItem(`$(error) ${this.errorMessage}`)];
    }
    if (!this.data) {
      return [this.messageItem('Run "AI Risk: Scan Workspace" to begin')];
    }

    const findings = this.data.findings || [];
    if (!element) {
      if (findings.length === 0) {
        return [this.messageItem('No code assessment risks detected')];
      }
      return SEVERITY_ORDER
        .map((severity) => {
          const count = findings.filter((finding) => finding.severity === severity).length;
          return { severity, count };
        })
        .filter(({ count }) => count > 0)
        .map(({ severity, count }) => {
          const item = new RiskTreeItem(
            `${this.titleCase(severity)} (${count})`,
            vscode.TreeItemCollapsibleState.Expanded,
            'severity',
            severity
          );
          item.iconPath = new vscode.ThemeIcon(SEVERITY_ICONS[severity] || 'circle-outline');
          item.tooltip = `${count} ${severity} risk finding(s)`;
          return item;
        });
    }

    if (element.kind === 'severity' && element.severity) {
      const areas = new Map<string, number>();
      findings
        .filter((finding) => finding.severity === element.severity)
        .forEach((finding) => areas.set(finding.area, (areas.get(finding.area) || 0) + 1));

      return Array.from(areas.entries())
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([area, count]) => {
          const item = new RiskTreeItem(
            `${area} (${count})`,
            vscode.TreeItemCollapsibleState.Expanded,
            'area',
            element.severity,
            area
          );
          item.iconPath = new vscode.ThemeIcon('folder');
          return item;
        });
    }

    if (element.kind === 'area' && element.severity && element.area) {
      return findings
        .filter((finding) => finding.severity === element.severity && finding.area === element.area)
        .sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line || a.rule_id.localeCompare(b.rule_id))
        .map((finding) => {
          const item = new RiskTreeItem(
            finding.rule_id || finding.title,
            vscode.TreeItemCollapsibleState.None,
            'finding',
            finding.severity,
            finding.area,
            finding
          );
          item.description = `${finding.file}:${finding.line}`;
          item.iconPath = new vscode.ThemeIcon(finding.control_source === 'llm' ? 'sparkle' : 'shield');
          item.command = {
            command: 'aiStackMapper.openRiskFinding',
            title: 'Open Risk Finding',
            arguments: [this.data!.root, finding],
          };
          item.tooltip = this.buildFindingTooltip(finding);
          return item;
        });
    }

    return [];
  }

  private buildFindingTooltip(finding: RiskFinding): vscode.MarkdownString {
    const control = finding.recommended_control || finding.suggestion || 'No control available.';
    const md = new vscode.MarkdownString(undefined, true);
    md.appendMarkdown(`**${finding.rule_id}**\n\n`);
    md.appendMarkdown(`Severity: \`${finding.severity}\`  \n`);
    md.appendMarkdown(`Feature: \`${finding.feature || 'general'}\`  \n`);
    md.appendMarkdown(`Source: \`${finding.source}\`  \n`);
    md.appendMarkdown(`Control source: \`${finding.control_source}\`  \n`);
    if (finding.llm_confidence) {
      md.appendMarkdown(`LLM confidence: \`${finding.llm_confidence}\`  \n`);
    }
    md.appendMarkdown(`\n${finding.title}\n\n`);
    if (finding.risk_explanation) {
      md.appendMarkdown(`**Risk explanation:** ${finding.risk_explanation}\n\n`);
    }
    md.appendMarkdown(`**Recommended control:** ${control}`);
    return md;
  }

  private messageItem(label: string): RiskTreeItem {
    return new RiskTreeItem(label, vscode.TreeItemCollapsibleState.None, 'message');
  }

  private titleCase(value: string): string {
    return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
  }
}

