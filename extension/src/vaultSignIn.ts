import * as vscode from 'vscode';
import * as crypto from 'crypto';
import * as http from 'http';
import { AddressInfo } from 'net';

export interface VaultSession {
  /** Full Authorization header value. */
  authorization: string;
  /** Tenant slug for the Org header. */
  org: string;
}

const SIGN_IN_TIMEOUT_MS = 5 * 60 * 1000;
// The launcher appends this many random characters to the encoded payload.
const AUTH_SUFFIX_LENGTH = 3;

/**
 * Decodes the launcher's `auth` query value: base64url JSON, reversed, plus a
 * random suffix. Carries access_token, refresh_token, realm_id, session_id, device_id.
 */
export function decodeLauncherAuth(auth: string): Record<string, string> {
  const base64 = auth.slice(0, -AUTH_SUFFIX_LENGTH).split('').reverse().join('');
  return JSON.parse(Buffer.from(base64, 'base64url').toString('utf8'));
}

/** Vault looks up the signing certificate by Org, which is the token's issuer. */
function tokenIssuer(token: string): string {
  try {
    return JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8')).iss || '';
  } catch {
    return '';
  }
}

/**
 * Opens the Solytics login launcher in the user's browser and waits for it to
 * redirect back to a one-shot listener on 127.0.0.1. The launcher accepts
 * loopback redirects and appends the session as `auth`; `state` ties the
 * callback to this sign-in so no other local page can hand us a token.
 */
export function signInWithBrowser(launcherUrl: string, cancel: vscode.CancellationToken): Promise<VaultSession> {
  const state = crypto.randomBytes(16).toString('hex');
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (done: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cancelSub.dispose();
      server.close();
      done();
    };

    const server = http.createServer((req, res) => {
      const url = new URL(req.url || '/', 'http://127.0.0.1');
      const auth = url.searchParams.get('auth');
      if (url.pathname !== '/callback' || url.searchParams.get('state') !== state || !auth) {
        res.writeHead(404).end();
        return;
      }
      try {
        const session = decodeLauncherAuth(auth);
        const token = session.access_token;
        const org = (token && tokenIssuer(token)) || session.realm_id;
        if (!token || !org) throw new Error('the login did not return a token and tenant');
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }).end(resultPage('Signed in to Vault', 'You can close this tab and return to VS Code.'));
        finish(() => resolve({ authorization: `Bearer ${token}`, org }));
      } catch (err: any) {
        res.writeHead(400, { 'Content-Type': 'text/html; charset=utf-8' }).end(resultPage('Sign-in failed', 'Return to VS Code and try again.'));
        finish(() => reject(new Error(`could not read the sign-in response: ${err.message}`)));
      }
    });

    const timer = setTimeout(() => finish(() => reject(new Error('timed out waiting for sign-in'))), SIGN_IN_TIMEOUT_MS);
    const cancelSub = cancel.onCancellationRequested(() => finish(() => reject(new Error('sign-in cancelled'))));
    server.on('error', (err) => finish(() => reject(err)));
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as AddressInfo;
      const target = new URL(launcherUrl);
      target.searchParams.set('redirect', `http://127.0.0.1:${port}/callback?state=${state}`);
      vscode.env.openExternal(vscode.Uri.parse(target.toString()));
    });
  });
}

function resultPage(title: string, message: string): string {
  return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>${title}</title></head>
<body style="font-family: system-ui, sans-serif; display: grid; place-items: center; height: 90vh; color: #274796;">
<div style="text-align: center;"><h2>${title}</h2><p style="color: #555;">${message}</p></div></body></html>`;
}
