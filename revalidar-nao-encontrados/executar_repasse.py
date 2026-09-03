from __future__ import annotations

import csv
import sys
from pathlib import Path

PASTA_ATUAL = Path(__file__).resolve().parent
RAIZ = PASTA_ATUAL.parent
REPASSE = PASTA_ATUAL / "dados" / "repasse.csv"

sys.path.insert(0, str(PASTA_ATUAL))
sys.path.insert(0, str(RAIZ))

import crm_tse_bot_revalidar as rv


def carregar_repasse():
    if not REPASSE.exists():
        raise RuntimeError(f"Repasse não encontrado: {REPASSE}")

    linhas = []

    with REPASSE.open("r", encoding="utf-8-sig", newline="") as arquivo:
        reader = csv.DictReader(arquivo)

        for linha in reader:
            cpf = rv.bot.only_digits(str(linha.get("cpf") or ""))

            if len(cpf) != 11:
                continue

            try:
                crm_id = int(linha.get("id") or 0)
            except (TypeError, ValueError):
                crm_id = 0

            foto = rv.PessoaFoto(
                crm_id=crm_id,
                cpf=cpf,
                nome=str(linha.get("nome") or "").strip(),
                mae=str(linha.get("mae") or "").strip(),
                nascimento=str(linha.get("nascimento") or "").strip(),
            )

            trabalho = rv.Trabalho(
                foto=foto,
                tentativas=int(linha.get("tentativas") or 0),
                ultimo_erro=str(linha.get("ultimo_erro") or ""),
            )

            linhas.append(trabalho)

    return linhas


def main():
    fila = carregar_repasse()

    print("=" * 72)
    print(" REPASSE - REVALIDAÇÃO DE NÃO ENCONTRADOS")
    print("=" * 72)
    print(f"Arquivo: {REPASSE}")
    print(f"CPFs no repasse: {len(fila)}")
    print()

    if not fila:
        print("Nenhum CPF no repasse.")
        return 0

    resposta = input(
        "Digite S para processar SOMENTE o repasse: "
    ).strip().upper()

    if resposta != "S":
        print("Encerrado sem executar.")
        return 0

    # reaproveita a configuração externa do módulo
    url_externa, token = rv.exigir_configuracao_externa()

    # usa execução única
    execucao_id = "repasse-manual"

    # usa operador 0 só para perfil/porta
    return rv.executar_operador_repasse_manual(
        operador=0,
        fila=fila,
        url_externa=url_externa,
        token=token,
        execucao_id=execucao_id,
        repasse_path=REPASSE,
    )


if __name__ == "__main__":
    raise SystemExit(main())