from __future__ import annotations

import csv
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright


TOTAL_OPERADORES = 4
PORTA_TSE_INICIAL = 9380

PASTA_ATUAL = Path(__file__).resolve().parent
RAIZ = PASTA_ATUAL.parent

os.environ["TSE_NAVEGADOR"] = "brave"
sys.path.insert(0, str(RAIZ))

import crm_tse_bot as bot  # noqa: E402


def ler_operador() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        raise SystemExit("Informe o operador: 0, 1, 2 ou 3.")

    operador = int(sys.argv[1])

    if not 0 <= operador < TOTAL_OPERADORES:
        raise SystemExit("Operador invalido. Use 0, 1, 2 ou 3.")

    return operador


def configurar_operador(operador: int) -> tuple[Path, Path]:
    dados = PASTA_ATUAL / "dados" / f"operador-{operador}"
    dados.mkdir(parents=True, exist_ok=True)

    repasse_csv = dados / "repasse.csv"

    bot.BASE_DIR = dados
    bot.PROFILE_DIR = dados / "perfil-crm"
    bot.TSE_PROFILE_DIR = dados / "perfil-tse-brave"
    bot.LOG_FILE = dados / "consultas.csv"
    bot.ERROR_LOG = dados / "bot_error.log"
    bot.DETAIL_LOG = dados / "execucao_detalhada.txt"
    bot.TSE_NAVEGADOR = "brave"
    bot.TSE_REMOTE_DEBUGGING_PORT = PORTA_TSE_INICIAL + operador

    return dados, repasse_csv


def normalizar_nascimento(valor: str) -> str:
    return bot.normalizar_nascimento_api(valor)


def carregar_repasse(caminho: Path) -> list[bot.Pessoa]:
    if not caminho.exists():
        return []

    pessoas: list[bot.Pessoa] = []
    cpfs_vistos: set[str] = set()

    with caminho.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as arquivo:
        for linha in csv.DictReader(arquivo):
            cpf = bot.only_digits(
                str(linha.get("cpf") or "")
            )

            if len(cpf) != 11:
                continue

            if cpf in cpfs_vistos:
                continue

            cpfs_vistos.add(cpf)

            try:
                crm_id = int(
                    linha.get("id") or 0
                )
            except (TypeError, ValueError):
                crm_id = 0

            pessoas.append(
                bot.Pessoa(
                    row_index=-1,
                    nome=str(
                        linha.get("nome") or ""
                    ).strip(),
                    cpf=cpf,
                    mae=str(
                        linha.get("mae") or ""
                    ).strip(),
                    nascimento=normalizar_nascimento(
                        str(
                            linha.get("nascimento")
                            or ""
                        )
                    ),
                    crm_id=(
                        crm_id
                        if crm_id > 0
                        else None
                    ),
                )
            )

    return pessoas


def salvar_repasse(
    caminho: Path,
    falhas: list[bot.Pessoa],
) -> None:
    caminho.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with caminho.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as arquivo:
        writer = csv.DictWriter(
            arquivo,
            fieldnames=[
                "id",
                "cpf",
                "nome",
                "mae",
                "nascimento",
            ],
        )

        writer.writeheader()

        for pessoa in falhas:
            writer.writerow(
                {
                    "id": pessoa.crm_id or "",
                    "cpf": pessoa.cpf,
                    "nome": pessoa.nome,
                    "mae": pessoa.mae,
                    "nascimento": (
                        pessoa.nascimento
                    ),
                }
            )


def executar() -> int:
    operador = ler_operador()

    dados, repasse_csv = (
        configurar_operador(
            operador
        )
    )

    fila = carregar_repasse(
        repasse_csv
    )

    print("=" * 72)
    print(
        f" REPASSE VARREDURA FINAL MULTI "
        f"- OPERADOR {operador}"
    )
    print("=" * 72)

    print(
        f"Arquivo: {repasse_csv}"
    )

    print(
        f"CPFs no repasse: {len(fila)}"
    )

    print(
        f"Porta TSE: "
        f"{bot.TSE_REMOTE_DEBUGGING_PORT}"
    )

    if not fila:
        print(
            "Nenhum CPF no repasse."
        )
        return 0

    resposta = input(
        "Digite S para executar somente "
        "este repasse: "
    ).strip().upper()

    if resposta != "S":
        print(
            "Encerrado sem consultar."
        )
        return 0

    context = None

    try:
        with sync_playwright() as playwright:
            context = (
                playwright.chromium
                .launch_persistent_context(
                    str(bot.PROFILE_DIR),
                    executable_path=(
                        bot.find_chrome_executable()
                    ),
                    headless=bot.HEADLESS,
                    slow_mo=bot.SLOW_MO_MS,
                    viewport={
                        "width": 1366,
                        "height": 768,
                    },
                    args=[
                        "--start-maximized"
                    ],
                )
            )

            crm = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            if not bot.ir_para(
                crm,
                bot.CRM_URL,
            ):
                raise RuntimeError(
                    "Nao consegui abrir o CRM."
                )

            bot.ensure_crm_ready(crm)

            print(
                "\nProcessando SOMENTE "
                "os CPFs do repasse..."
            )

            falhas = bot.rodar_fila(
                playwright,
                context,
                crm,
                fila,
                f"repasse-{operador} ",
            )

            salvar_repasse(
                repasse_csv,
                falhas,
            )

            print("=" * 72)
            print(
                f"Operador {operador} "
                f"finalizado."
            )

            print(
                f"Tentados: {len(fila)}"
            )

            print(
                f"Ainda no repasse: "
                f"{len(falhas)}"
            )

            print(
                f"Arquivo atualizado: "
                f"{repasse_csv}"
            )

            return 0

    except KeyboardInterrupt:
        print(
            "\nInterrompido."
        )

        return 130

    except Exception:
        dados.mkdir(
            parents=True,
            exist_ok=True,
        )

        with bot.ERROR_LOG.open(
            "a",
            encoding="utf-8",
        ) as arquivo:
            arquivo.write(
                f"\n{'=' * 70}\n"
                f"{datetime.now().isoformat(timespec='seconds')}"
                f" | erro geral no repasse\n"
            )

            arquivo.write(
                traceback.format_exc()
            )

        print(
            "\nErro geral. Veja:"
        )

        print(
            bot.ERROR_LOG
        )

        return 1

    finally:
        bot.encerrar_sessao_tse()

        if context is not None:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(executar())