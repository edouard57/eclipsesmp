const fs = require('fs-extra')
const path = require('path')

/**
 * Vanilla Minecraft writes screenshots to <gameDir>/screenshots. gameDir
 * mirrors ProcessBuilder's own path.join(ConfigManager.getInstanceDirectory(), serverId).
 *
 * @param {string} instanceDirectory ConfigManager.getInstanceDirectory().
 * @param {string} serverId The selected server's id.
 */
exports.getScreenshotsDir = function(instanceDirectory, serverId){
    return path.join(instanceDirectory, serverId, 'screenshots')
}

/**
 * List screenshots for the given server, newest first.
 *
 * @param {string} instanceDirectory ConfigManager.getInstanceDirectory().
 * @param {string} serverId The selected server's id.
 * @returns {Array.<{name: string, path: string, mtime: number}>}
 */
exports.scanForScreenshots = function(instanceDirectory, serverId){
    const dir = exports.getScreenshotsDir(instanceDirectory, serverId)
    if(!fs.existsSync(dir)){
        return []
    }
    return fs.readdirSync(dir)
        .filter(f => /\.(png|jpg|jpeg)$/i.test(f))
        .map(f => {
            const full = path.join(dir, f)
            try {
                return { name: f, path: full, mtime: fs.statSync(full).mtimeMs }
            } catch(err) {
                // Deleted/moved between readdir and stat (e.g. still being
                // written by Minecraft) -- just skip it.
                return null
            }
        })
        .filter(s => s != null)
        .sort((a, b) => b.mtime - a.mtime)
}
