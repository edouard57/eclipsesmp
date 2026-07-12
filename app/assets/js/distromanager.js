const { DistributionAPI } = require('helios-core/common')

const ConfigManager = require('./configmanager')

// Back on raw.githubusercontent.com. jsDelivr's own gh proxy got stuck
// resolving the @main ref for over an hour on 2026-07-12 (their resolve
// API returned version: null while GitHub's API confirmed the branch was
// fine) with purges having no effect, so it's not reliably faster than
// GitHub's occasional multi-shard staleness. Re-evaluate if raw github
// staleness becomes a problem again.
exports.REMOTE_DISTRO_URL = 'https://raw.githubusercontent.com/edouard57/eclipsesmp/main/distribution.json'

const api = new DistributionAPI(
    ConfigManager.getLauncherDirectory(),
    null, // Injected forcefully by the preloader.
    null, // Injected forcefully by the preloader.
    exports.REMOTE_DISTRO_URL,
    false
)

exports.DistroAPI = api