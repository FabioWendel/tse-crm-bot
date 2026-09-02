from __future__ import annotations

import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error, Page, TimeoutError


MAX_PAGINAS_INVENTARIO = 1000
RAIZ_PROJETO = Path(__file__).resolve().parent.parent

os.environ["TSE_NAVEGADOR"] = "brave"
sys.path.insert(0, str(RAIZ_PROJETO))
import crm_tse_bot as bot  # noqa: E402


def texto_da_primeira_linha(page: Page) -> str:
    try:
        return bot.clean(page.locator("table tbody tr").first.inner_text(timeout=5000))
    except (TimeoutError, Error):
        return ""


def ir_para_proxima_pagina(page: Page) -> bool:
    candidatos = (
        page.get_by_role("button", name=re.compile(r"^(pr[óo]xim[ao]|next|seguinte)", re.I)).first,
        page.get_by_role("link", name=re.compile(r"^(pr[óo]xim[ao]|next|seguinte)", re.I)).first,
        page.locator("button[aria-label*='rox' i], a[aria-label*='rox' i]").first,
        page.locator("button, a").filter(has_text=re.compile(r"^\s*(›|»|>)\s*$")).first,
    )
    primeira_linha = texto_da_primeira_linha(page)
    for alvo in candidatos:
        try:
            if not alvo.is_visible(timeout=1000) or not alvo.is_enabled(timeout=1000):
                continue
            alvo.click(timeout=8000)
        except (TimeoutError, Error):
            continue

        page.wait_for_timeout(1500)
        if texto_da_primeira_linha(page) != primeira_linha:
            return True
    return False


def inventariar_pendentes(page: Page) -> list[bot.Pessoa]:
    """Fotografa todas as paginas antes de iniciar qualquer consulta."""
    for tentativa in range(1, bot.TENTATIVAS_POR_PESSOA + 1):
        try:
            bot.voltar_para_pendentes(page)
            bot.maximizar_por_pagina(page)
            encontrados: dict[str, bot.Pessoa] = {}

            for pagina in range(1, MAX_PAGINAS_INVENTARIO + 1):
                antes = len(encontrados)
                for row_index in range(bot.total_rows(page)):
                    pessoa = bot.read_person_from_row(page, row_index)
                    if pessoa:
                        encontrados.setdefault(pessoa.cpf, pessoa)

                novos = len(encontrados) - antes
                print(f"  pagina {pagina}: +{novos} pendente(s) (total {len(encontrados)})")
                if not ir_para_proxima_pagina(page) or novos == 0:
                    break

            if encontrados:
                return list(encontrados.values())
            print(f"Pendentes veio vazio (tentativa {tentativa}/{bot.TENTATIVAS_POR_PESSOA}).")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if bot.navegador_caiu(exc):
                raise
            print(f"Leitura de Pendentes falhou ({tentativa}/{bot.TENTATIVAS_POR_PESSOA}): {bot.resumo_erro(exc)}")

        if tentativa < bot.TENTATIVAS_POR_PESSOA:
            bot.recuperar_crm(page)
    return []


def perguntar_configuracao() -> tuple[int, int, int, int, int]:
    print("Modo SOMENTE PENDENTES: uma unica fila, sem divisao por API.")
    limite = bot.ler_inteiro(
        "Quantos Pendentes no maximo nesta rodada? [Enter/0 = todos] ",
        minimo=0,
        maximo=100000,
        padrao=0,
    )
    print("Sem teto: vou ate o fim da fotografia de Pendentes." if limite == 0 else f"Teto: {limite} CPF(s).")
    pausar_a_cada = bot.ler_inteiro(
        "Fazer uma pausa a cada quantas pessoas? [Enter = 10, minimo = 1] ",
        minimo=1,
        maximo=100000,
        padrao=10,
    )
    duracao = bot.ler_inteiro(
        "Quantos segundos deve durar cada pausa? [Enter = 10, minimo = 1] ",
        minimo=1,
        maximo=3600,
        padrao=10,
    )
    print(f"Pausa configurada: {duracao}s a cada {pausar_a_cada} pessoa(s).")
    return 0, 1, limite, pausar_a_cada, duracao


def executar() -> int:
    # O main normal continua cuidando de CRM, TSE, salvamento e repasse. Somente
    # a origem da fila e a pergunta inicial sao substituidas nesta execucao.
    bot.inventariar_eleitores_api = inventariar_pendentes
    bot.novos_eleitores_da_api = lambda *_args, **_kwargs: []
    bot.perguntar_operador = perguntar_configuracao
    bot.FILA_ORIGEM_LABEL = "Fotografia da aba Pendentes"
    bot.FILA_VAZIA_MSG = "Nao encontrei pessoas na aba Pendentes."
    bot.FILA_FATIA_LABEL = "pendente(s) na fila"
    bot.FILA_CONFIRMACAO_MSG = "Antes do TSE, cada CPF sera confirmado novamente em Pendentes."

    print("=" * 64)
    print(" CRM x TSE - MODO NORMAL - SOMENTE PENDENTES")
    print("=" * 64)
    print("Use esta janela sozinha. Nao rode junto com filas divididas.")
    print("O CAPTCHA continua manual.")
    print("=" * 64)
    print()

    try:
        if "--teste" in sys.argv[1:]:
            return bot.autoteste()
        bot.main()
        print("\nConcluido.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrompido por voce. O que ja foi salvo continua no CRM.")
        return 130
    except Exception:
        with bot.ERROR_LOG.open("a", encoding="utf-8") as arquivo:
            arquivo.write(f"\n{'=' * 70}\n{datetime.now().isoformat(timespec='seconds')} | erro geral\n")
            arquivo.write(traceback.format_exc())
        print(f"\nDeu erro. Salvei o detalhe em: {bot.ERROR_LOG}")
        print("\n".join(traceback.format_exc().splitlines()[-8:]))
        return 1
    finally:
        bot.encerrar_sessao_tse()


if __name__ == "__main__":
    raise SystemExit(executar())
