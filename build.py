r"""Gera o pacote distribuivel para os operadores.

    Windows:  .\.venv\Scripts\python.exe build.py   ->  dist\CRM-TSE-Bot.exe
    macOS:    ./.venv/bin/python build.py           ->  dist/CRM-TSE-Bot.dmg

PyInstaller NAO faz compilacao cruzada: o pacote do macOS precisa ser gerado
num macOS, e o do Windows num Windows. Este script cuida dos dois casos, mas
cada um tem de rodar na sua propria plataforma.

Nao empacotamos navegador: o bot dirige o Google Chrome instalado na maquina,
que portanto e pre-requisito nas duas plataformas.
"""

import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).parent
NOME = "CRM-TSE-Bot"

# O .command e o que torna o binario clicavel no Finder: dois cliques abrem o
# Terminal, que e onde o operador digita o numero e ve o andamento.
COMMAND_MACOS = f"""#!/bin/bash
# Abre o bot numa janela de Terminal. Mantenha este arquivo ao lado do binario.
cd "$(dirname "$0")"
./{NOME}
"""


def rodar(comando: list[str]) -> int:
    print(" ".join(comando), "\n")
    return subprocess.run(comando, cwd=RAIZ).returncode


def comando_pyinstaller() -> list[str]:
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        # console: o operador digita o numero do operador e resolve o CAPTCHA
        "--console",
        "--name",
        NOME,
        # o playwright carrega um driver Node em playwright/driver/, que o
        # PyInstaller nao acha seguindo apenas os imports
        "--collect-all",
        "playwright",
        # openpyxl so e usado pelo conversor de CSV, fica de fora
        "--exclude-module",
        "openpyxl",
        "--exclude-module",
        "tkinter",
        str(RAIZ / "crm_tse_bot.py"),
    ]


def empacotar_dmg() -> int:
    """Monta dist/CRM-TSE-Bot.dmg com o binario, o atalho e o LEIA-ME."""
    binario = RAIZ / "dist" / NOME
    if not binario.exists():
        print(f"Nao encontrei o binario em {binario}.")
        return 1

    estagio = RAIZ / "build" / "dmg"
    shutil.rmtree(estagio, ignore_errors=True)
    estagio.mkdir(parents=True)

    shutil.copy2(binario, estagio / NOME)
    (estagio / NOME).chmod(0o755)

    atalho = estagio / f"{NOME}.command"
    atalho.write_text(COMMAND_MACOS, encoding="utf-8")
    atalho.chmod(0o755)

    leia_me = RAIZ / "LEIA-ME-OPERADOR.md"
    if leia_me.exists():
        shutil.copy2(leia_me, estagio / "LEIA-ME.md")

    dmg = RAIZ / "dist" / f"{NOME}.dmg"
    dmg.unlink(missing_ok=True)
    return rodar(
        [
            "hdiutil",
            "create",
            "-volname",
            NOME,
            "-srcfolder",
            str(estagio),
            "-ov",
            "-format",
            "UDZO",
            str(dmg),
        ]
    )


def main() -> int:
    if sys.platform.startswith("win"):
        alvo, rotulo = RAIZ / "dist" / f"{NOME}.exe", "Windows"
    elif sys.platform == "darwin":
        alvo, rotulo = RAIZ / "dist" / f"{NOME}.dmg", "macOS"
    else:
        print(f"Plataforma nao suportada para empacotamento: {sys.platform}")
        return 1

    print(f"Gerando pacote para {rotulo}.\n")
    for pasta in ("build", "dist"):
        shutil.rmtree(RAIZ / pasta, ignore_errors=True)

    codigo = rodar(comando_pyinstaller())
    if codigo != 0:
        return codigo

    if sys.platform == "darwin":
        codigo = empacotar_dmg()
        if codigo != 0:
            return codigo

    if not alvo.exists():
        print(f"Build terminou sem erro, mas {alvo.name} nao apareceu em dist/.")
        return 1

    print(f"\nPronto: {alvo}")
    print(f"Tamanho: {alvo.stat().st_size / 1_048_576:.1f} MB")

    if sys.platform == "darwin":
        print("\nO binario nao esta assinado. No primeiro uso, o operador precisa")
        print("clicar com o botao direito no .command e escolher Abrir, ou rodar:")
        print(f"  xattr -dr com.apple.quarantine /caminho/para/{NOME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
