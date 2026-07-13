#!/usr/bin/env python3
"""Regenerates distribution.json from mods.json (fetched from Modrinth) and the Fabric loader manifest.
Run this again whenever a mod version is bumped or a new mod is added.
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODS = json.load(open(os.path.join(BASE, "scripts", "mods.json")))
FABRIC_LIBS = json.load(open(os.path.join(BASE, "scripts", "fabric_libs.json")))
MOD_TITLES = json.load(open(os.path.join(BASE, "scripts", "mod_titles.json")))

MC_VERSION = "26.2"
FABRIC_LOADER_VERSION = "0.19.3"
FABRIC_LOADER_MD5 = "881e0e9f53a11b9ad468b47afd678bfd"
FABRIC_LOADER_SIZE = 1976502
FABRIC_PROFILE_MD5 = "66d31338f5e5b9cabf161a30329a8595"
FABRIC_PROFILE_SIZE = 2778

# Placeholders to fill in once the server + repo hosting exist.
SERVER_ADDRESS = "smp.cubi-mc.fr:25565"
SERVER_ICON_URL = "https://raw.githubusercontent.com/edouard57/eclipsesmp/main/branding/server-icon.png"

# Clean on-disk name (the Modrinth filename has a space in it). Must match
# the resourcePacks entry ProcessBuilder writes into options.txt.
FULLBRIGHT_FILENAME = "Fullbright-UB-6.0.zip"


def fabric_mod_module(key, tier_prefix=None, required=True, default=True):
    m = MODS[key]
    title = MOD_TITLES.get(key, key)
    return {
        "id": f"modrinth:{key}:{m['version_number']}",
        "name": f"{tier_prefix + ' ' if tier_prefix else ''}{title}",
        "type": "FabricMod",
        "required": {"value": required, "def": default},
        "artifact": {
            "size": m["size"],
            "MD5": m["md5"],
            "url": m["url"],
        },
    }


def resourcepack_file_module(key, filename, required=False, default=True):
    m = MODS[key]
    title = MOD_TITLES.get(key, key)
    return {
        "id": f"modrinth:{key}:{m['version_number']}",
        "name": title,
        "type": "File",
        "required": {"value": required, "def": default},
        "artifact": {
            "size": m["size"],
            "MD5": m["md5"],
            "path": f"resourcepacks/{filename}",
            "url": m["url"],
        },
    }


def shader_file_module(key, path, required=False, default=False):
    m = MODS[key]
    return {
        "id": f"modrinth:{key}:{m['version_number']}",
        "name": f"[PC puissant] {m['filename']}",
        "type": "File",
        "required": {"value": required, "def": default},
        "artifact": {
            "size": m["size"],
            "MD5": m["md5"],
            "path": path,
            "url": m["url"],
        },
    }


fabric_loader_module = {
    "id": f"net.fabricmc:fabric-loader:{FABRIC_LOADER_VERSION}",
    "name": "Fabric Loader",
    "type": "Fabric",
    "artifact": {
        "size": FABRIC_LOADER_SIZE,
        "MD5": FABRIC_LOADER_MD5,
        "url": f"https://maven.fabricmc.net/net/fabricmc/fabric-loader/{FABRIC_LOADER_VERSION}/fabric-loader-{FABRIC_LOADER_VERSION}.jar",
    },
    "subModules": [
        {
            "id": f"{MC_VERSION}-fabric-{FABRIC_LOADER_VERSION}",
            "name": "Fabric (version.json)",
            "type": "VersionManifest",
            "artifact": {
                "size": FABRIC_PROFILE_SIZE,
                "MD5": FABRIC_PROFILE_MD5,
                "url": f"https://meta.fabricmc.net/v2/versions/loader/{MC_VERSION}/{FABRIC_LOADER_VERSION}/profile/json",
            },
        },
        # Fabric Loader's own runtime dependencies (ASM + sponge-mixin). These are
        # NOT read from the VersionManifest above by HeliosLauncher's classpath
        # builder -- only explicit "Library" submodules are added to -cp. Without
        # these, KnotClient crashes at startup with "ASM not detected on the
        # classpath". Keep this list in sync with the "libraries" array returned
        # by https://meta.fabricmc.net/v2/versions/loader/<mc>/<loader>/profile/json
        *[
            {
                "id": maven_id,
                "name": maven_id.split(":")[1],
                "type": "Library",
                "artifact": {
                    "size": lib["size"],
                    "MD5": lib["md5"],
                    "url": lib["url"],
                },
            }
            for maven_id, lib in FABRIC_LIBS.items()
        ],
    ],
}

modules = [
    fabric_loader_module,
    # --- Core (required for everyone, safe on any PC) ---
    fabric_mod_module("fabric-api", required=True),
    fabric_mod_module("sodium", required=True),
    fabric_mod_module("lithium", required=True),
    fabric_mod_module("ferrite-core", required=True),
    # Gameplay feature required on both client and server (elytra trims need
    # to be in sync, and the mod needs Fabric Language Kotlin as a library).
    fabric_mod_module("fabric-language-kotlin", required=True),
    fabric_mod_module("elytra-trims", required=True),
    fabric_mod_module("defixus", required=True),
    # --- Confort (optional, on by default even on weak PCs: pure optimizations
    #     or QoL with no real tradeoff, same spirit as the required core above) ---
    fabric_mod_module("dynamic-fps", tier_prefix="[Confort]", required=False, default=True),
    fabric_mod_module("jei", tier_prefix="[Confort]", required=False, default=True),
    fabric_mod_module("entityculling", tier_prefix="[Confort]", required=False, default=True),
    fabric_mod_module("immediatelyfast", tier_prefix="[Confort]", required=False, default=True),
    fabric_mod_module("appleskin", tier_prefix="[Confort]", required=False, default=True),
    fabric_mod_module("mouse-tweaks", tier_prefix="[Confort]", required=False, default=True),
    fabric_mod_module("libipn", tier_prefix="[Confort]", required=False, default=False),
    fabric_mod_module("inventory-profiles-next", tier_prefix="[Confort]", required=False, default=False),
    fabric_mod_module("jade", tier_prefix="[Confort]", required=False, default=True),
    fabric_mod_module("simple-voice-chat", tier_prefix="[Confort]", required=False, default=True),
    # Library required by Zoomify.
    fabric_mod_module("yacl", required=True),
    fabric_mod_module("zoomify", tier_prefix="[Confort]", required=False, default=True),
    # --- Interface (minimap / carte / menu des mods, optionnel, actif par defaut) ---
    fabric_mod_module("xaeros-minimap", tier_prefix="[Interface]", required=False, default=True),
    fabric_mod_module("xaeros-world-map", tier_prefix="[Interface]", required=False, default=True),
    fabric_mod_module("placeholder-api", tier_prefix="[Interface]", required=True),
    fabric_mod_module("modmenu", tier_prefix="[Interface]", required=True),
    # --- Visuel (purement cosmetique, desactive par defaut car question de gout) ---
    fabric_mod_module("not-enough-animations", tier_prefix="[Visuel]", required=False, default=False),
    # --- PC puissant (optional, off by default: real CPU/GPU/RAM cost) ---
    fabric_mod_module("c2me-fabric", tier_prefix="[PC puissant]", required=False, default=False),
    fabric_mod_module("iris", tier_prefix="[PC puissant]", required=False, default=False),
    fabric_mod_module("distanthorizons", tier_prefix="[PC puissant]", required=False, default=False),
    shader_file_module("complementary-reimagined", "shaderpacks/ComplementaryReimagined_r5.8.1.zip"),
    resourcepack_file_module("fullbright-ub", FULLBRIGHT_FILENAME, required=False, default=True),
    {
        "id": "eclipse-smp:servers.dat",
        "name": "Serveur pre-rempli dans le menu multijoueur",
        "type": "File",
        "artifact": {
            "size": 12586,
            "MD5": "de48c9d91966da0abcda7f32bc2709e3",
            "path": "servers.dat",
            "url": "https://raw.githubusercontent.com/edouard57/eclipsesmp/main/branding/servers.dat",
        },
    },
]

distribution = {
    "version": "1.0.0",
    "rss": "https://eclipsesmp.cubi-mc.fr/news.xml",
    "servers": [
        {
            "id": "eclipse-smp",
            "name": "Eclipse SMP",
            "description": "Serveur SMP Eclipse. Fabric 26.2 avec un pack d'optimisation, plus des mods de confort au choix selon la puissance de ton PC.",
            "icon": SERVER_ICON_URL,
            "version": "1.0.0",
            "address": SERVER_ADDRESS,
            "minecraftVersion": MC_VERSION,
            "mainServer": True,
            "autoconnect": True,
            "javaOptions": {
                # MC 26.2's own JVM arg template includes
                # --sun-misc-unsafe-memory-access=allow, unrecognized by JDK 21
                # ("Unrecognized option" -> exit code 1, silent to the user).
                # Verified working on JDK 25; require at least that.
                "supported": ">=25",
                "suggestedMajor": 25,
                "ram": {"recommended": 4096, "minimum": 2048},
            },
            "modules": modules,
        }
    ],
}

out_path = os.path.join(BASE, "distribution.json")
with open(out_path, "w") as f:
    json.dump(distribution, f, indent=4, ensure_ascii=False)
    f.write("\n")

print(f"Wrote {out_path}")
