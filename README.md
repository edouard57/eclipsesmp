# Eclipse SMP Launcher

Launcher Minecraft dédié au serveur **Eclipse SMP** (Fabric 26.2). Basé sur
[HeliosLauncher](https://github.com/dscalzi/HeliosLauncher) (MIT, voir
[UPSTREAM_README.md](./UPSTREAM_README.md) et [LICENSE.txt](./LICENSE.txt)).

Le launcher se connecte automatiquement au serveur Eclipse SMP, télécharge
Java si besoin, installe Fabric + le pack de mods, et laisse le joueur
choisir des mods optionnels selon la puissance de son PC.

## Ce qui est fait / à faire

- [x] Base du launcher (Electron, compte Microsoft, gestion Java auto)
- [x] `distribution.json` généré avec Fabric 26.2 + pack d'optimisation
- [x] 3 paliers de mods optionnels (Confort / PC moyen / PC puissant)
- [ ] Adresse réelle du serveur (`distribution.json` → `servers[0].address`)
- [ ] Hébergement de `distribution.json` sur GitHub (voir plus bas)
- [ ] Logo / icônes propres (`build/icon.png`, `branding/server-icon.png`)
- [ ] Build Windows (voir plus bas)

## Lancer en local (Linux, dev)

```bash
npm install
npm start
```

## Comment ça marche : `distribution.json`

Le launcher lit un fichier `distribution.json` distant à chaque démarrage
pour savoir quel serveur proposer, quelle version de Fabric utiliser, et
quels mods installer. C'est le fichier à la racine du repo.

Il est généré par `scripts/build_distribution.py` à partir de
`scripts/mods.json` (métadonnées + hash MD5 récupérés depuis l'API
Modrinth). Pour changer un mod ou en ajouter un : éditer les constantes en
haut de `scripts/build_distribution.py`, puis relancer :

```bash
python3 scripts/build_distribution.py
```

### Paliers de mods (optionnels, activables par le joueur dans le launcher)

Tous les joueurs ont d'office (obligatoires) : **Fabric API, Sodium,
Lithium, FerriteCore** (optimisations sans effet de bord, aucune raison de
les désactiver).

Ensuite, dans l'écran des mods du launcher, chaque joueur peut cocher/décocher :

| Palier | Mods | Activé par défaut |
|---|---|---|
| **Confort** (tous PC) | Dynamic FPS (baisse la charge quand la fenêtre est en arrière-plan) | Oui |
| **PC moyen** | EntityCulling, ImmediatelyFast | Non |
| **PC puissant** | C2ME (multithread chunks/worldgen), Iris, Distant Horizons, shader **Complementary Reimagined** | Non |

Il n'y a pas de bouton "preset en un clic" pour l'instant — le joueur coche
les mods de son palier à la main (c'est prévu par HeliosLauncher nativement,
aucun code à écrire). Si tu veux un vrai bouton de préréglage plus tard, il
faudra toucher `app/assets/js/scripts/settings.js` (écran des mods).

### Pourquoi pas de shaders sur "PC puissant" par défaut avant ?

Minecraft 26.2 est sorti mi-juin 2026 ; la plupart des packs de shaders
n'avaient pas encore de version compatible au moment de la config initiale.
**Complementary Reimagined r5.8.1** est maintenant disponible pour 26.2 et
est inclus. Si tu veux en changer, cherche le mod sur
[Modrinth](https://modrinth.com) (type "shader", filtre version = `26.2`,
loader = `iris`), ajoute-le dans `scripts/mods.json` / `build_distribution.py`.

## Héberger `distribution.json` (GitHub)

Pas besoin de GitHub Pages : `raw.githubusercontent.com` suffit.

1. Crée un repo GitHub (public) et pousse ce projet dedans.
2. Récupère l'URL brute du fichier :
   `https://raw.githubusercontent.com/edouard57/eclipsesmp/main/distribution.json`
3. Remplace `edouard57/eclipsesmp` par cette valeur dans :
   - `app/assets/js/distromanager.js` (`REMOTE_DISTRO_URL`)
   - `dev-app-update.yml` (`owner`/`repo`, pour l'auto-update en dev)
   - `package.json` (`homepage`, `repository.url`, `bugs.url`)
   - `app/assets/js/scripts/uicore.js` et `settings.js` (liens de mise à jour macOS/atom)
4. Chaque fois que tu modifies `distribution.json` (nouveau mod, adresse
   serveur...), il suffit de push sur `main` : le launcher relit le fichier
   à chaque démarrage, pas besoin de republier le launcher lui-même.

## Adresse du serveur

Édite `scripts/mods.json`/`build_distribution.py` → constante
`SERVER_ADDRESS`, puis régénère `distribution.json`. Format : `ip:port` ou
`domaine:port` (port par défaut Minecraft = 25565, peut être omis si
standard).

## Build Windows

Le launcher (Electron) tourne nativement sur Windows sans rien changer au
code. Deux options :

- **Depuis Windows** : `npm install` puis `npm run dist:win` (nécessite
  Node.js sur la machine Windows).
- **Cross-build depuis Linux** : `npm run dist:win` fonctionne aussi depuis
  Linux via `electron-builder` + Wine, mais c'est plus fragile (à tester).
  Le plus fiable reste de builder directement sur Windows ou via CI
  (GitHub Actions, un workflow existe déjà dans `.github/`).

## Dossier de données du launcher

Le launcher stocke tout (config, Java téléchargé, mods, instances) dans un
dossier dédié :

- Linux/macOS : `~/.eclipse-smp-launcher`
- Windows : `%APPDATA%\.eclipse-smp-launcher`

(renommé depuis `.helioslauncher` d'origine pour ne pas entrer en conflit
avec une éventuelle install de HeliosLauncher vanilla sur la même machine).

## Logs du launcher (pour diagnostiquer un bug)

Le launcher lui-même (pas Minecraft) écrit ce qui apparaît normalement
dans la console DevTools (Ctrl+Shift+I) dans un fichier, un par session,
avec rotation automatique (les 10 plus récents sont gardés) :

- Linux : `~/.config/Eclipse SMP Launcher/logs/`
- Windows : `%APPDATA%\Eclipse SMP Launcher\logs\`
- macOS : `~/Library/Application Support/Eclipse SMP Launcher/logs/`

En cas de bug launcher (pas de crash Minecraft, ça c'est
`instances/eclipse-smp/logs/latest.log` -- voir plus haut), demande le
fichier `launcher-<date>.log` le plus récent.
