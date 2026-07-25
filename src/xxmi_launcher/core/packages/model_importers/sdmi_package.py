import re
import os
import json
import sqlite3
import logging

import wmi 

from dataclasses import dataclass, field
from typing import Dict, Union, Optional, Tuple, List
from pathlib import Path

import core.path_manager as Paths
import core.event_manager as Events
import core.config_manager as Config

from core.locale_manager import L
from core.package_manager import PackageMetadata

from core.utils.ini_handler import IniHandler, IniHandlerSettings
from core.packages.model_importers.model_importer import ModelImporterPackage, ModelImporterConfig, Version
from core.packages.migoto_package import MigotoManagerConfig

log = logging.getLogger(__name__)


@dataclass
class SDMIConfig(ModelImporterConfig):
    game_exe_names: List[str] = field(default_factory=lambda: ['STARDIVE.exe'])
    process_exe_names: List[str] = field(default_factory=lambda: ['BigCat-Win64-Shipping.exe'])
    game_folder_names: List[str] = field(default_factory=lambda: ['Mongil Stardive Game'])
    game_folder_children: List[str] = field(default_factory=lambda: ['BigCat'])
    importer_folder: str = 'SDMI/'
    launch_options: str = ''
    xxmi_dll_init_delay: int = 500
    # Cached Netmarble launcher args captured from STARDIVE.exe command line (skip WMI wait on next launch)
    netmarble_account_env: str = ''
    d3dx_ini: Dict[
        str, Dict[str, Dict[str, Union[str, int, float, Dict[str, Union[str, int, float]]]]]
    ] = field(default_factory=lambda: {
        'core': {
            'Loader': {
                'loader': 'XXMI Launcher.exe',
            },
        },
        'enforce_rendering': {
            'Rendering': {
                'texture_hash': 1,
                'track_texture_updates': 1,
                'track_region_hashes': 0,
                'allow_buffer_resize': 1,
            },
        },
        'calls_logging': {
            'Logging': {
                'calls': {'on': 1, 'off': 0},
            },
        },
        'debug_logging': {
            'Logging': {
                'debug': {'on': 1, 'off': 0},
            },
        },
        'mute_warnings': {
            'Logging': {
                'show_warnings': {'on': 0, 'off': 1},
            },
        },
        'enable_hunting': {
            'Hunting': {
                'hunting': {'on': 2, 'off': 0},
            },
        },
        'dump_shaders': {
            'Hunting': {
                'marking_actions': {'on': 'clipboard hlsl asm regex', 'off': 'clipboard'},
            },
        },
    })


@dataclass
class SDMIPackageConfig:
    Importer: SDMIConfig = field(
        default_factory=lambda: SDMIConfig()
    )
    Migoto: MigotoManagerConfig = field(
        default_factory=lambda: MigotoManagerConfig()
    )


class SDMIPackage(ModelImporterPackage):
    def __init__(self):
        super().__init__(PackageMetadata(
            package_name='SDMI',
            auto_load=False,
            github_repo_owner='hondaislife420',
            github_repo_name='SDMI-Package',
            asset_version_pattern=r'.*(\d\.\d\.\d).*',
            asset_name_format='SDMI-Package-v%s.zip',
            signature_pattern=r'^## Signature[\r\n]+- ((?:[A-Za-z0-9+\/]{4})*(?:[A-Za-z0-9+\/]{4}|[A-Za-z0-9+\/]{3}=|[A-Za-z0-9+\/]{2}={2})$)',
            signature_public_key='MHYwEAYHKoZIzj0CAQYFK4EEACIDYgAEAAJ1/6g4OGFNW+77YolSYtVd4j8STOyPIxdJNU7ifNWankRpZ1/DS0kxgQa5LpxrxIZQiqgRTewzzMiLWUZlvDGu+9RK7mlrV0/Ucl7ujlyjjHQnIKj957Q10ytw0R36',
            exit_after_update=False,
            installation_path='SDMI/',
            requirements=['XXMI'],
        ))
        self.use_hook: bool = False

    def get_installed_version(self):
        try:
            return str(Version(Config.Importers.SDMI.Importer.importer_path / 'Core' / 'SDMI' / 'Stardive-Model-Importer.ini', pattern=r'^global \$sdmi_version = (\d+)\.*(\d)(\d*)'))
        except Exception as e:
            return ''

    def normalize_game_path(self, game_path: Path) -> Path:
        if not game_path.is_absolute():
            raise ValueError(L('error_game_path_not_absolute', 'Failed to normalize path {path}: Path is not absolute!').format(path=game_path))

        if (game_path / 'STARDIVE.exe').is_file():
            return game_path

        game_path_original = game_path

        for path in game_path.rglob('*.exe'):
            if path.is_file() and path.name == 'STARDIVE.exe':
                return Path(path).parent

        for i in range(len(game_path.parents)):
            game_path = game_path.parent
            for path in game_path.iterdir():
                if path.is_file() and path.name == 'STARDIVE.exe':
                    return Path(path).parent

        raise ValueError(L(
            'error_mongil_stardive_exe_not_found',
            'Failed to normalize path {path}: STARDIVE.exe not found!'
        ).format(path=game_path_original))

    def validate_game_path(self, game_folder):
        game_path = super().validate_game_path(game_folder)
        game_path = self.normalize_game_path(game_path)
        exe_path = game_path / 'STARDIVE.exe'
        if not exe_path.is_file():
            raise ValueError(L(
                'error_sdmi_game_folder_missing_files',
                'Game folder must contain STARDIVE.exe and a BigCat folder!'
            ))
        if 'BigCat' not in [x.name for x in game_path.iterdir() if x.is_dir()]:
            raise ValueError(L('error_game_folder_missing_folder', 'Game folder must contain BigCat folder!'))
        return game_path

    def validate_game_exe_path(self, game_path: Path) -> Path:
        for game_exe_name in Config.Active.Importer.process_exe_names:
            game_exe_path = game_path / 'BigCat' / 'Binaries' / 'Win64' / game_exe_name
            if game_exe_path.is_file():
                return game_exe_path
        raise ValueError(L('error_game_exe_not_found', 'Game executable {exe_name} not found!').format(
            exe_name=' / '.join(Config.Active.Importer.process_exe_names)))

    def get_netmarble_account(self) -> str:
        """Block until STARDIVE.exe is created and capture its command-line args after the exe path."""
        c = wmi.WMI()
        watcher = c.Win32_Process.watch_for("creation")

        log.info('Waiting for STARDIVE.exe to capture Netmarble account env...')
        Events.Fire(Events.Application.StatusUpdate(
            status=L('status_waiting_netmarble_account', 'Waiting for STARDIVE.exe (start game via Netmarble launcher once)...')
        ))

        found_str = ""
        while found_str == "":
            p = watcher()
            if p.Name and p.Name.lower() == "stardive.exe":
                s = p.CommandLine or ""
                parts = s.split('"', 2)
                if len(parts) >= 3:
                    found_str = parts[2].strip()
                else:
                    found_str = s.strip()
        log.info('Captured Netmarble account env from STARDIVE.exe')
        return found_str

    def get_or_capture_netmarble_account(self) -> str:
        """Return cached account env from config, or capture once and persist it."""
        cached = (Config.Importers.SDMI.Importer.netmarble_account_env or '').strip()
        if cached:
            log.debug('Using cached Netmarble account env')
            return cached

        account_env = self.get_netmarble_account()
        Config.Importers.SDMI.Importer.netmarble_account_env = account_env
        # Keep Active in sync if SDMI is the current importer
        if Config.Launcher.active_importer == 'SDMI':
            Config.Active.Importer.netmarble_account_env = account_env
        Config.Config.save()
        log.info('Saved Netmarble account env to config')
        return account_env

    def parse_netmarble_account_args(self, account_env: str) -> List[str]:
        """Turn captured command-line tail into argv for BigCat-Win64-Shipping.exe."""
        # Command line tail is typically: " -arg1 -arg2 ..." or " arg1 arg2 arg3"
        tokens = [t for t in account_env.split(' ') if t]
        if len(tokens) < 3:
            raise ValueError(L(
                'error_sdmi_invalid_netmarble_account',
                'Netmarble account env is invalid (expected at least 3 args).\n\n'
                'Clear Settings → re-capture by deleting netmarble_account_env from config, '
                'or start the game once via the Netmarble launcher while XXMI is waiting.'
            ))
        # Original logic used indices 1..3 (skipped a leading empty/flag token in some builds).
        # Prefer first 3 non-empty tokens if there are exactly 3; otherwise keep indices 1-3 when 4+.
        if len(tokens) == 3:
            return tokens
        return [tokens[1], tokens[2], tokens[3]]

    def get_start_cmd(self, game_path: Path) -> Tuple[Path, List[str], Optional[str]]:
        game_exe_path = self.validate_game_exe_path(game_path)
        account_env = self.get_or_capture_netmarble_account()
        args = self.parse_netmarble_account_args(account_env)
        return game_exe_path, args, str(game_path)

    def initialize_game_launch(self, game_path: Path):
        return
