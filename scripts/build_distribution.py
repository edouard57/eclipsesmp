#!/usr/bin/env python3
"""Regenerates distribution.json from mods.json (fetched from Modrinth) and the Fabric loader manifest.
Run this again whenever a mod version is bumped or a new mod is added.
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODS = json.load(open(os.path.join(BASE, "scripts", "mods.json")))

MC_VERSION = "26.2"
FABRIC_LOADER_VERSION = "0.19.3"
FABRIC_LOADER_MD5 = "881e0e9f53a11b9ad468b47afd678bfd"
FABRIC_LOADER_SIZE = 1976502
FABRIC_PROFILE_MD5 = "66d31338f5e5b9cabf161a30329a8595"
FABRIC_PROFILE_SIZE = 2778

# Placeholders to fill in once the server + repo hosting exist.
SERVER_ADDRESS = "smp.cubi-mc.fr:25565"
SERVER_ICON_URL = "https://raw.githubusercontent.com/edouard57/eclipsesmp/main/branding/server-icon.png"


def fabric_mod_module(key, tier_prefix=None, required=True, default=True):
    m = MODS[key]
    return {
        "id": f"modrinth:{key}:{m['version_number']}",
        "name": f"{tier_prefix + ' ' if tier_prefix else ''}{m.get('name', key)}",
        "type": "FabricMod",
        "required": {"value": required, "def": default},
        "artifact": {
            "size": m["size"],
            "MD5": m["md5"],
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
        }
    ],
}

modules = [
    fabric_loader_module,
    # --- Core (required for everyone, safe on any PC) ---
    fabric_mod_module("fabric-api", required=True),
    fabric_mod_module("sodium", required=True),
    fabric_mod_module("lithium", required=True),
    fabric_mod_module("ferrite-core", required=True),
    # --- Confort (optional, on by default even on weak PCs) ---
    fabric_mod_module("dynamic-fps", tier_prefix="[Confort]", required=False, default=True),
    # --- PC moyen (optional, off by default) ---
    fabric_mod_module("entityculling", tier_prefix="[PC moyen]", required=False, default=False),
    fabric_mod_module("immediatelyfast", tier_prefix="[PC moyen]", required=False, default=False),
    # --- PC puissant (optional, off by default) ---
    fabric_mod_module("c2me-fabric", tier_prefix="[PC puissant]", required=False, default=False),
    fabric_mod_module("iris", tier_prefix="[PC puissant]", required=False, default=False),
    fabric_mod_module("distanthorizons", tier_prefix="[PC puissant]", required=False, default=False),
    shader_file_module("complementary-reimagined", "shaderpacks/ComplementaryReimagined_r5.8.1.zip"),
]

distribution = {
    "version": "1.0.0",
    "rss": "",
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
                "supported": ">=21",
                "suggestedMajor": 21,
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
