import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as path from 'path';

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
  categories: { [category: string]: ComponentData[] };
}

export const LLM_API_KEY_SECRET = "aiStackMapper.llmApiKey";

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

  async scan(workspaceRoot: string): Promise<ScanData> {
    const cfg = vscode.workspace.getConfiguration('aiStackMapper');
    const enrich = cfg.get<boolean>('enrichWithLLM', false);

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

    const args = ['-m', 'ai_stack_scanner.cli', '--path', workspaceRoot, '--format', 'json'];

    if (enrich) {
      const apiKey = await this.secrets.get(LLM_API_KEY_SECRET);
      if (!apiKey) {
        throw new Error(
          'AI enrichment is enabled ("aiStackMapper.enrichWithLLM") but no API key is set. ' +
            'Run "AI Stack: Set LLM API Key" first, or disable enrichment in Settings.'
        );
      }
      // Passed via env, never as a CLI arg, so the key never shows up in a
      // process listing (e.g. `ps`/Task Manager).
      env.AI_STACK_LLM_API_KEY = apiKey;
      const baseUrl = cfg.get<string>('llmBaseUrl', '');
      if (baseUrl) {
        env.AI_STACK_LLM_BASE_URL = baseUrl;
      }
      const model = cfg.get<string>('llmModel', '');
      if (model) {
        env.AI_STACK_LLM_MODEL = model;
      }
      args.push('--enrich');
    }

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
          reject(new Error(`Scanner exited with code ${code}.\n${stderr}`));
          return;
        }
        try {
          resolve(JSON.parse(stdout) as ScanData);
        } catch (err) {
          reject(new Error(`Could not parse scanner output as JSON: ${err}\n${stdout.slice(0, 500)}`));
        }
      });
    });
  }
}
