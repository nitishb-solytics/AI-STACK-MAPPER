import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as path from 'path';
import * as fs from 'fs/promises';

export interface Occurrence {
  file: string;
  line: number;
  match_type: string;
  confidence: string;
  detail: string;
  deployment_target?: string;
  context_hint?: string;
  prompt_hint?: string;
}

export interface AiEnrichment {
  purpose: string;
  usage_description: string;
  expected_output: string;
  model: string;
}

export interface ComponentData {
  category: string;
  name: string;
  package: string;
  confidence: string;
  count: number;
  deployment_targets?: string[];
  ai_enrichment?: AiEnrichment;
  occurrences: Occurrence[];
}

export interface ScanData {
  root: string;
  generated_at: string;
  scanned_files: number;
  skipped_files: string[];
  total_components: number;
  local_agents?: LocalAgentData[];
  categories: { [category: string]: ComponentData[] };
}

export interface LocalAgentData {
  key: string;
  name: string;
  score: number;
  files: string[];
  dependency_files?: string[];
  components: { [bucket: string]: string[] };
  evidence: Array<{
    category: string;
    component: string;
    file: string;
    line: number;
    match_type: string;
    detail: string;
    package?: string;
    attribution?: 'direct_path' | 'local_import';
    import_depth?: number;
    ownership_confidence?: 'high' | 'medium' | 'low';
  }>;
}

export interface VaultLocalAgentPayload {
  external_id: string;
  content_hash: string;
  key: string;
  name: string;
  score: number;
  files: string[];
  dependency_files?: string[];
  components: { [bucket: string]: string[] };
  evidence: LocalAgentData['evidence'];
}

export interface VaultPublishPayload {
  source_type: string;
  provider_key: string;
  repo_name: string;
  repo_path: string;
  repo_url: string;
  branch: string;
  stack: ScanData;
  risk: Record<string, never>;
  local_agents: VaultLocalAgentPayload[];
  discover: false;
  include_risks: boolean;
}

export interface StackScanResult {
  markdownPath: string;
  jsonPath: string;
  data: ScanData;
}

export interface RiskFinding {
  severity: string;
  area: string;
  file: string;
  line: number;
  title: string;
  suggestion: string;
  rule_id: string;
  feature: string;
  source: string;
  control_source: string;
  risk_explanation?: string;
  recommended_control?: string;
  safer_code?: string;
  llm_confidence?: string;
  is_valid_risk?: boolean;
  evidence_snippet?: string;
}

export interface RiskReportData {
  root: string;
  generated_at: string;
  scanned_files: number;
  changed_only: boolean;
  fail_on: string;
  status: string;
  risk_scan_mode: string;
  report_title: string;
  llm_model: string;
  severity_counts: { [severity: string]: number };
  findings: RiskFinding[];
  skipped_files: string[];
  llm_warnings: string[];
}

export interface RiskScanResult {
  markdownPath: string;
  jsonPath: string;
  data: RiskReportData;
}

/**
 * Spawns the bundled `ai_stack_scanner` Python package as a subprocess.
 * No `pip install` is required: we point PYTHONPATH at the copy of the
 * package shipped inside the extension (see extension/python/).
 */
export class ScannerBridge {
  constructor(
    private readonly extensionPath: string,
    private readonly secrets: vscode.SecretStorage
  ) {}

  private get pythonPath(): string {
    return vscode.workspace.getConfiguration('aiStackMapper').get<string>('pythonPath') || 'python3';
  }

  private get bundledEnginePath(): string {
    return path.join(this.extensionPath, 'python');
  }

  async scan(workspaceRoot: string): Promise<StackScanResult> {
    const env: NodeJS.ProcessEnv = { ...process.env };
    const existing = env.PYTHONPATH ? `${env.PYTHONPATH}${path.delimiter}` : '';
    env.PYTHONPATH = `${existing}${this.bundledEnginePath}`;

    // The extension controls enrichment entirely via its own settings +
    // Secret Storage (below) -- never via a `.env` file. Explicitly point
    // the scanner at a nonexistent env-file path so it can't accidentally
    // pick up an `AI_STACK_*` variable from a `.env` in the *scanned*
    // workspace root (cli.py's default `.env` lookup is relative to its
    // process cwd, which here is the target repo being scanned, not this
    // extension). Without this, a scanned repo could otherwise plant its
    // own `.env` to silently influence enrichment settings.
    env.AI_STACK_ENV_FILE = '';

    const markdownPath = path.join(workspaceRoot, 'AI_STACK.md');
    const jsonPath = path.join(workspaceRoot, 'ai-stack-report.json');

    const args = [
      '-m',
      'ai_stack_scanner.cli',
      '--path',
      workspaceRoot,
      '--markdown-output',
      markdownPath,
      '--json-output',
      jsonPath,
    ];

    return new Promise((resolve, reject) => {
      let proc: cp.ChildProcessWithoutNullStreams;
      try {
        proc = cp.spawn(this.pythonPath, args, { env, cwd: workspaceRoot });
      } catch (err: any) {
        reject(new Error(`Failed to launch "${this.pythonPath}": ${err.message}`));
        return;
      }

      let stderr = '';
      proc.stderr.on('data', (d) => (stderr += d.toString()));

      proc.on('error', (err) => {
        reject(
          new Error(
            `Could not run Python ("${this.pythonPath}"): ${err.message}. ` +
              `If Python isn't on your PATH, set "aiStackMapper.pythonPath" in Settings.`
          )
        );
      });

      proc.on('close', (code) => {
        void (async () => {
          if (code !== 0) {
            reject(new Error(`Scanner exited with code ${code}.\n${stderr}`));
            return;
          }
          try {
            const raw = await fs.readFile(jsonPath, 'utf8');
            const data = JSON.parse(raw) as ScanData;
            resolve({ markdownPath, jsonPath, data });
          } catch (err: any) {
            reject(new Error(`Scanner completed but could not read ${path.basename(jsonPath)}: ${err.message}`));
          }
        })();
      });
    });
  }

  /**
   * Builds the Vault codebase-discovery payload (including per-agent
   * `external_id`/`content_hash`) via the shared `ai_stack_scanner.vault_publish`
   * Python module, so every discovery entry point (extension, local-path CLI,
   * future GitHub-fetch job) derives agent identity identically.
   */
  async buildVaultPayload(
    workspaceRoot: string,
    stackJsonPath: string,
    options: { repoUrl?: string; branch?: string; selectedKeys?: string[] }
  ): Promise<VaultPublishPayload> {
    const env: NodeJS.ProcessEnv = { ...process.env };
    const existing = env.PYTHONPATH ? `${env.PYTHONPATH}${path.delimiter}` : '';
    env.PYTHONPATH = `${existing}${this.bundledEnginePath}`;

    const args = [
      '-m',
      'ai_stack_scanner.vault_publish',
      '--repo-root',
      workspaceRoot,
      '--stack-json',
      stackJsonPath,
      '--repo-url',
      options.repoUrl || '',
      '--branch',
      options.branch || '',
      '--selected-keys',
      (options.selectedKeys || []).join(','),
    ];

    return new Promise((resolve, reject) => {
      let proc: cp.ChildProcessWithoutNullStreams;
      try {
        proc = cp.spawn(this.pythonPath, args, { env, cwd: workspaceRoot });
      } catch (err: any) {
        reject(new Error(`Failed to launch "${this.pythonPath}": ${err.message}`));
        return;
      }

      let stdout = '';
      let stderr = '';
      proc.stdout.on('data', (d) => (stdout += d.toString()));
      proc.stderr.on('data', (d) => (stderr += d.toString()));

      proc.on('error', (err) => {
        reject(
          new Error(
            `Could not run Python ("${this.pythonPath}"): ${err.message}. ` +
              `If Python isn't on your PATH, set "aiStackMapper.pythonPath" in Settings.`
          )
        );
      });

      proc.on('close', (code) => {
        if (code !== 0) {
          reject(new Error(`Vault payload build exited with code ${code}.\n${stderr}`));
          return;
        }
        try {
          resolve(JSON.parse(stdout) as VaultPublishPayload);
        } catch (err: any) {
          reject(new Error(`Could not parse Vault payload output: ${err.message}`));
        }
      });
    });
  }

  async scanRisks(workspaceRoot: string): Promise<RiskScanResult> {
    const cfg = vscode.workspace.getConfiguration('aiStackMapper');
    const useLlm = cfg.get<boolean>('riskUseLLM', false);
    const failOn = cfg.get<string>('riskFailOn', 'high') || 'high';
    const riskLlmMaxFindings = Math.max(1, cfg.get<number>('riskLlmMaxFindings', 25) || 25);
    const riskLlmMinSeverity = cfg.get<string>('riskLlmMinSeverity', 'high') || 'high';
    const env: NodeJS.ProcessEnv = { ...process.env };
    const existing = env.PYTHONPATH ? `${env.PYTHONPATH}${path.delimiter}` : '';
    env.PYTHONPATH = `${existing}${this.bundledEnginePath}`;
    env.AI_STACK_ENV_FILE = '';

    const markdownPath = path.join(workspaceRoot, 'AI_RISK_REPORT.md');
    const jsonPath = path.join(workspaceRoot, 'ai-risk-report.json');
    const args = [
      '-m',
      'ai_stack_scanner.risk_cli',
      '--path',
      workspaceRoot,
      '--markdown-output',
      markdownPath,
      '--json-output',
      jsonPath,
      '--fail-on',
      failOn,
      '--no-fail',
    ];

    if (useLlm) {
      throw new Error(
        'Risk LLM controls are not enabled in this Vault-local-agent package. Disable "aiStackMapper.riskUseLLM".'
      );
    }

    return new Promise((resolve, reject) => {
      let proc: cp.ChildProcessWithoutNullStreams;
      try {
        proc = cp.spawn(this.pythonPath, args, { env, cwd: workspaceRoot });
      } catch (err: any) {
        reject(new Error(`Failed to launch "${this.pythonPath}": ${err.message}`));
        return;
      }

      let stderr = '';
      proc.stderr.on('data', (d) => (stderr += d.toString()));
      proc.on('error', (err) => {
        reject(
          new Error(
            `Could not run Python ("${this.pythonPath}"): ${err.message}. ` +
              `If Python isn't on your PATH, set "aiStackMapper.pythonPath" in Settings.`
          )
        );
      });
      proc.on('close', (code) => {
        void (async () => {
          if (code !== 0) {
            reject(new Error(`Risk scanner exited with code ${code}.\n${stderr}`));
            return;
          }
          try {
            const raw = await fs.readFile(jsonPath, 'utf8');
            resolve({ markdownPath, jsonPath, data: JSON.parse(raw) as RiskReportData });
          } catch (err: any) {
            reject(new Error(`Risk scanner completed but could not read ${path.basename(jsonPath)}: ${err.message}`));
          }
        })();
      });
    });
  }
}
