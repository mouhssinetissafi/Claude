import {app, safeStorage} from 'electron';
import fs from 'node:fs';
import path from 'node:path';

interface StoredSecrets {
  anthropic?: string;
  elevenlabs?: string;
  elevenlabsVoice?: string;
}

export class SecretStore {
  private readonly filePath: string;

  constructor() {
    this.filePath = path.join(app.getPath('userData'), 'secrets.json');
  }

  load(): StoredSecrets {
    if (!fs.existsSync(this.filePath) || !safeStorage.isEncryptionAvailable()) return {};
    try {
      const raw = JSON.parse(fs.readFileSync(this.filePath, 'utf8')) as Record<string, string>;
      const out: StoredSecrets = {};
      for (const [key, value] of Object.entries(raw)) {
        const plain = safeStorage.decryptString(Buffer.from(value, 'base64'));
        if (key === 'anthropic' || key === 'elevenlabs' || key === 'elevenlabsVoice') out[key] = plain;
      }
      return out;
    } catch {
      return {};
    }
  }

  save(update: StoredSecrets): StoredSecrets {
    if (!safeStorage.isEncryptionAvailable()) throw new Error('Secure credential storage is not available on this system');
    const merged = {...this.load(), ...update};
    fs.mkdirSync(path.dirname(this.filePath), {recursive: true});
    const encrypted: Record<string, string> = {};
    for (const [key, value] of Object.entries(merged)) {
      if (!value) continue;
      encrypted[key] = safeStorage.encryptString(value).toString('base64');
    }
    fs.writeFileSync(this.filePath, JSON.stringify(encrypted, null, 2), 'utf8');
    return merged;
  }

  toEnvironment(secrets = this.load()): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = {};
    if (secrets.anthropic) env.ANTHROPIC_API_KEY = secrets.anthropic;
    if (secrets.elevenlabs) env.ELEVENLABS_API_KEY = secrets.elevenlabs;
    if (secrets.elevenlabsVoice) env.ELEVENLABS_VOICE_ID = secrets.elevenlabsVoice;
    return env;
  }

  status(secrets = this.load()): {anthropic: boolean; elevenlabs: boolean; elevenlabs_voice: boolean} {
    return {
      anthropic: Boolean(secrets.anthropic),
      elevenlabs: Boolean(secrets.elevenlabs),
      elevenlabs_voice: Boolean(secrets.elevenlabsVoice),
    };
  }
}
