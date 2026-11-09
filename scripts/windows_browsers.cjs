// Publish checksum-verified browsers from the parent PowerShell process.
// Python prepares the archives; application startup probes every browser afterwards.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const root = path.resolve(process.argv[2]);
const runtime = path.join(root, 'yutaixian_data', 'local_dev');
const requestFile = path.join(runtime, 'browser-install-request.json');
const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const inside = (base, target) => {
  const relative = path.relative(base, target);
  return relative !== '' && !relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative);
};

async function sha256(file) {
  const digest = crypto.createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) digest.update(chunk);
  return digest.digest('hex');
}

async function renameWithRetry(source, destination) {
  for (let attempt = 0; ; attempt++) {
    try { fs.renameSync(source, destination); return; }
    catch (error) {
      if (!['EPERM', 'EACCES', 'EBUSY'].includes(error.code) || attempt >= 59) throw error;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  }
}

async function main() {
  if (process.platform !== 'win32' || process.arch !== 'x64') throw new Error('Windows x64 is required.');
  const request = read(requestFile);
  if (path.resolve(request.root).toLowerCase() !== root.toLowerCase()) throw new Error('Browser request root differs.');
  const manifest = read(path.join(root, 'launcher', 'windows', 'dependencies.json'));
  const driver = path.join(root, 'backend-web', '.venv', 'Lib', 'site-packages', 'playwright', 'driver', 'package', 'lib');
  const { utils } = require(path.join(driver, 'coreBundle.js'));
  const { yauzl } = require(path.join(driver, 'utilsBundle.js'));
  const base = path.join(root, 'yutaixian_data', 'windows', 'browsers');
  fs.mkdirSync(base, { recursive: true });
  if (fs.lstatSync(base).isSymbolicLink()) throw new Error('Browser cache must not be a junction or symlink.');
  const canonicalBase = fs.realpathSync(base);

  const validateZip = archive => new Promise((resolve, reject) => {
    yauzl.open(archive, { lazyEntries: true }, (error, zip) => {
      if (error) return reject(error);
      zip.on('error', reject);
      zip.on('end', resolve);
      zip.on('entry', entry => {
        const name = entry.fileName.replaceAll('\\', '/');
        const symlink = ((entry.externalFileAttributes >>> 16) & 0o170000) === 0o120000;
        if (name.startsWith('/') || name.includes(':') || name.split('/').includes('..') || symlink) {
          zip.close(); reject(new Error('Unsafe browser archive member.')); return;
        }
        zip.readEntry();
      });
      zip.readEntry();
    });
  });

  for (const [name, version] of request.browsers) {
    const entry = manifest.browsers.find(item => item.name === name && item.version === version);
    if (!entry || !/^[a-z-]+$/.test(name) || !/^[0-9.]+$/.test(version) || !/^[a-f0-9]{64}$/i.test(entry.sha256)) {
      throw new Error('Browser revision or SHA256 is missing from the manifest.');
    }
    const destination = path.join(base, name.replaceAll('-', '_') + '-' + version);
    const executable = path.resolve(destination, entry.executable);
    if (!inside(destination, executable)) throw new Error('Browser executable escapes its directory.');
    const archive = path.join(root, 'yutaixian_data', 'windows', 'downloads', name + '-' + version + '.zip');
    if (await sha256(archive) !== entry.sha256.toLowerCase()) throw new Error(name + ' SHA256 mismatch; not extracted.');
    await validateZip(archive);
    const previous = fs.readdirSync(base).filter(name => name.startsWith(path.basename(destination) + '.extract-'));
    let stage = previous.map(name => path.join(base, name)).find(candidate => {
      try {
        return !fs.lstatSync(candidate).isSymbolicLink() &&
          read(path.join(candidate, '.installed.json')).sha256 === entry.sha256 &&
          fs.statSync(path.join(candidate, entry.executable)).isFile() &&
          fs.existsSync(path.join(candidate, 'INSTALLATION_COMPLETE'));
      } catch { return false; }
    });
    if (!stage) {
      stage = destination + '.extract-' + crypto.randomUUID();
      fs.mkdirSync(stage);
      await utils.extractZip(archive, { dir: stage });
      if (!fs.statSync(path.join(stage, entry.executable)).isFile()) throw new Error('Unexpected browser archive layout.');
      fs.writeFileSync(path.join(stage, '.installed.json'), JSON.stringify({ sha256: entry.sha256, executable: entry.executable }));
      fs.writeFileSync(path.join(stage, 'INSTALLATION_COMPLETE'), '');
    }
    if (fs.existsSync(destination)) {
      if (fs.lstatSync(destination).isSymbolicLink() || !inside(canonicalBase, fs.realpathSync(destination))) {
        throw new Error('Refusing to move a browser directory outside the cache.');
      }
      await renameWithRetry(destination, destination + '.incomplete-' + crypto.randomUUID());
    }
    await renameWithRetry(stage, destination);
    console.log('Installed verified browser component: ' + name + ' ' + version);
  }
  fs.unlinkSync(requestFile);
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
