import { Client, GatewayIntentBits, AttachmentBuilder } from 'discord.js';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import 'dotenv/config';

const run = promisify(execFile);

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
  ],
});

client.once('clientReady', () => console.log(`up as ${client.user.tag}`));

client.on('messageCreate', async (msg) => {
  if (msg.author.bot) return;

  const pdf = msg.attachments.find(a => a.name?.toLowerCase().endsWith('.pdf'));
  if (!pdf) return;

  const dir = await mkdtemp(join(tmpdir(), 'a4-'));
  try {
    const inPath  = join(dir, 'in.pdf');
    const outPath = join(dir, 'out.pdf');

    const res = await fetch(pdf.url);
    await writeFile(inPath, Buffer.from(await res.arrayBuffer()));

    await run('bash', ['./pdf-to-a4.sh', inPath, outPath], { timeout: 30_000 });

    await msg.reply({
      files: [new AttachmentBuilder(outPath, {
        name: pdf.name.replace(/\.pdf$/i, '-a4.pdf'),
      })],
    });
  } catch (err) {
    console.error(err);
    await msg.reply(`Couldn't convert that: ${err.stderr?.trim() ?? err.message}`);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

client.login(process.env.DISCORD_TOKEN);