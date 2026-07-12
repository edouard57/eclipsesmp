const { DistributionAPI } = require('helios-core/common')

const ConfigManager = require('./configmanager')

// Using jsDelivr instead of raw.githubusercontent.com: GitHub's raw CDN
// showed persistent multi-shard cache inconsistency (some requests kept
// hitting a stale shard indefinitely, even with cache-busting query
// params). jsDelivr's cache can be force-purged after each push via
// scripts/purge_cdn.py, giving us actual control over freshness.
exports.REMOTE_DISTRO_URL = 'https://cdn.jsdelivr.net/gh/edouard57/eclipsesmp@main/distribution.json'

const api = new DistributionAPI(
    ConfigManager.getLauncherDirectory(),
    null, // Injected forcefully by the preloader.
    null, // Injected forcefully by the preloader.
    exports.REMOTE_DISTRO_URL,
    false
)

exports.DistroAPI = api