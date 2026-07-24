import * as vscode from 'vscode';
import { ScanData, ComponentData, Occurrence } from './scannerBridge';

const CATEGORY_LABELS: { [k: string]: string } = {
  LLM: 'LLM Providers',
  MCP: 'MCP (Model Context Protocol)',
  TOOL: 'Tools / Function Calling',
  AGENT_FRAMEWORK: 'Agent & Orchestration Frameworks',
  VECTOR_STORE: 'Vector Stores / Memory',
};

const CATEGORY_ICONS: { [k: string]: string } = {
  LLM: 'hubot',
  MCP: 'plug',
  TOOL: 'tools',
  AGENT_FRAMEWORK: 'organization',
  VECTOR_STORE: 'database',
};

const CONFIDENCE_ICONS: { [k: string]: string } = {
  high: 'pass-filled',
  medium: 'circle-filled',
  low: 'circle-outline',
};

type NodeKind = 'message' | 'category' | 'component' | 'occurrence';

export class AiStackTreeItem extends vscode.TreeItem {
  constructor(
    label: string,
    collapsibleState: vscode.TreeItemCollapsibleState,
    public readonly kind: NodeKind,
    public readonly categoryKey?: string,
    public readonly component?: ComponentData
  ) {
    super(label, collapsibleState);
  }
}

export class AiStackTreeProvider implements vscode.TreeDataProvider<AiStackTreeItem> {
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<AiStackTreeItem | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private data: ScanData | undefined;
  private errorMessage: string | undefined;

  setData(data: ScanData): void {
    this.data = data;
    this.errorMessage = undefined;
    this._onDidChangeTreeData.fire();
  }

  setError(message: string): void {
    this.errorMessage = message;
    this.data = undefined;
    this._onDidChangeTreeData.fire();
  }

  getScanData(): ScanData | undefined {
    return this.data;
  }

  getTreeItem(element: AiStackTreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: AiStackTreeItem): AiStackTreeItem[] {
    if (this.errorMessage) {
      return [this.messageItem(`$(error) ${this.errorMessage}`)];
    }
    if (!this.data) {
      return [this.messageItem('Run "AI Stack: Scan Workspace" to begin')];
    }

    if (!element) {
      const categories = Object.keys(this.data.categories).filter(
        (cat) => this.data!.categories[cat].length > 0
      );
      if (categories.length === 0) {
        return [this.messageItem('No AI-stack components detected')];
      }
      return categories.map((cat) => {
        const count = this.data!.categories[cat].length;
        const item = new AiStackTreeItem(
          `${CATEGORY_LABELS[cat] || cat} (${count})`,
          vscode.TreeItemCollapsibleState.Expanded,
          'category',
          cat
        );
        item.iconPath = new vscode.ThemeIcon(CATEGORY_ICONS[cat] || 'symbol-misc');
        return item;
      });
    }

    if (element.kind === 'category' && element.categoryKey) {
      const components = this.data.categories[element.categoryKey] || [];
      return components.map((comp) => {
        const item = new AiStackTreeItem(
          comp.name,
          vscode.TreeItemCollapsibleState.Collapsed,
          'component',
          element.categoryKey,
          comp
        );
        item.description = `${comp.count} \u00b7 ${comp.confidence}`;
        item.iconPath = new vscode.ThemeIcon(CONFIDENCE_ICONS[comp.confidence] || 'circle-outline');
        const deployment = comp.deployment_targets?.length ? comp.deployment_targets.join(' + ') : 'cloud';
        let tooltipMd =
          `**${comp.name}**\n\nConfidence: \`${comp.confidence}\`\n\nDeployment: \`${deployment}\`\n\n` +
          `Package: \`${comp.package || 'n/a'}\`\n\nOccurrences: ${comp.count}`;
        if (comp.ai_enrichment) {
          const ai = comp.ai_enrichment;
          tooltipMd += `\n\n---\n_AI-generated (unverified, model: ${ai.model})_`;
          if (ai.purpose) tooltipMd += `\n\n**Purpose:** ${ai.purpose}`;
          if (ai.usage_description) tooltipMd += `\n\n**Usage:** ${ai.usage_description}`;
          if (ai.expected_output) tooltipMd += `\n\n**Expected output:** ${ai.expected_output}`;
        }
        item.tooltip = new vscode.MarkdownString(tooltipMd);
        return item;
      });
    }

    if (element.kind === 'component' && element.component) {
      return element.component.occurrences
        .slice()
        .sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line)
        .map((occ) => {
          const item = new AiStackTreeItem(`${occ.file}:${occ.line}`, vscode.TreeItemCollapsibleState.None, 'occurrence');
          item.description = occ.match_type;
          item.iconPath = new vscode.ThemeIcon('file-code');
          item.command = {
            command: 'aiStackMapper.openOccurrence',
            title: 'Open',
            arguments: [this.data!.root, occ],
          };
          return item;
        });
    }

    return [];
  }

  private messageItem(label: string): AiStackTreeItem {
    return new AiStackTreeItem(label, vscode.TreeItemCollapsibleState.None, 'message');
  }
}

export function occurrenceToLocation(root: string, occ: Occurrence) {
  return { root, occ };
}
