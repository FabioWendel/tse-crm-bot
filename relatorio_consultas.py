r"""Analisa o consultas.csv e separa as pessoas por situacao.

Roda em qualquer maquina que tenha o repositorio -- basta ter o consultas.csv
ao lado. Nao acessa CRM, TSE nem rede: le o CSV e classifica com as MESMAS
regras do bot (importa crm_tse_bot), entao o relatorio nunca diverge do que o
robo faria hoje.

Uso:
    python relatorio_consultas.py                  # usa ./consultas.csv
    python relatorio_consultas.py caminho/arq.csv  # usa outro arquivo

Gera, ao lado do CSV de origem:
    consultas-reativar-biometria.csv   quem foi inativado pela regra antiga de
                                       biometria e hoje deve ficar ATIVO
    consultas-inativar.csv             quem as regras atuais mandam inativar
    consultas-nao-encontrados.csv      quem o TSE nao localizou

Os tres saem no padrao consultas*.csv, que o .gitignore ja cobre: eles contem
CPF e nome e nao podem ser versionados nem compartilhados fora do combinado.
"""

import csv
import sys
from collections import Counter
from pathlib import Path

from crm_tse_bot import (
    ResultadoTse,
    is_irregular,
    motivo_inativacao,
    normalize_text,
)

# Redacoes de biometria pendente/desatualizada. Todas seguem votando normal.
TERMOS_BIOMETRIA = ("NAO COLETADA", "NAO COLETADO", "DESATUALIZADA", "DESATUALIZADO", "SEM BIOMETRIA")

COLUNAS_SAIDA = ["cpf", "nome", "data_consulta", "status", "comunicado", "acao"]


def carregar(origem: Path) -> list[dict]:
    if not origem.exists():
        raise SystemExit(f"Nao encontrei {origem}. Rode o bot antes, ou informe o caminho do CSV.")

    with origem.open(encoding="utf-8-sig", newline="") as arquivo:
        return list(csv.DictReader(arquivo))


def resultado_da_linha(linha: dict) -> ResultadoTse:
    """Remonta o ResultadoTse a partir da linha do CSV, para reusar as regras."""
    return ResultadoTse(
        texto_para_crm=linha["resultado"],
        status=linha["status"],
        comunicado=linha["comunicado"],
        irregular=linha["irregular"] == "sim",
        encontrado=linha["encontrado"] == "sim",
    )


def tem_biometria_pendente(linha: dict) -> bool:
    blob = normalize_text(" ".join((linha["status"], linha["comunicado"], linha["resultado"])))
    return "BIOMETRIA" in blob and any(termo in blob for termo in TERMOS_BIOMETRIA)


def gravar(destino: Path, pessoas: list[dict]) -> None:
    with destino.open("w", encoding="utf-8-sig", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=COLUNAS_SAIDA)
        escritor.writeheader()
        escritor.writerows(pessoas)


def linha_de_saida(linha: dict, acao: str) -> dict:
    return {
        "cpf": linha["cpf"],
        "nome": linha["nome"],
        "data_consulta": linha["data_hora"],
        "status": linha["status"],
        "comunicado": linha["comunicado"],
        "acao": acao,
    }


def main() -> int:
    origem = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("consultas.csv")
    linhas = carregar(origem)

    # Uma pessoa pode ter sido consultada mais de uma vez: vale a mais recente.
    # O CSV e append-only e cronologico, entao a ultima ocorrencia vence.
    por_cpf: dict[str, dict] = {}
    for linha in linhas:
        if linha.get("cpf"):
            por_cpf[linha["cpf"]] = linha
    unicas = list(por_cpf.values())

    reativar: list[dict] = []
    inativar: list[dict] = []
    nao_encontrados: list[dict] = []
    motivos = Counter()

    for linha in unicas:
        resultado = resultado_da_linha(linha)

        if not resultado.encontrado:
            nao_encontrados.append(linha_de_saida(linha, "TSE nao localizou"))
            continue

        motivo = motivo_inativacao(resultado)
        motivos[motivo or "(permanece ativo)"] += 1

        if motivo:
            inativar.append(linha_de_saida(linha, f"Inativar: {motivo}"))
        elif tem_biometria_pendente(linha):
            # Antes isso inativava como "Problema na biometria". Hoje nao:
            # o titulo segue regular e a pessoa vota. Quem ja foi inativado
            # por essa regra precisa voltar a ativo.
            reativar.append(linha_de_saida(linha, "REATIVAR - biometria nao inativa mais"))

    print(f"Origem: {origem}")
    print(f"{len(linhas)} consulta(s), {len(unicas)} pessoa(s) distinta(s)\n")

    print("Situacao pelas regras atuais:")
    print(f"  {len(nao_encontrados):>4}  nao localizados no TSE")
    for motivo, quantidade in motivos.most_common():
        print(f"  {quantidade:>4}  {motivo}")

    print("\nIrregulares (para conferencia):", sum(1 for l in unicas if is_irregular(l["status"], l["comunicado"], l["resultado"])))

    saidas = (
        (origem.with_name("consultas-reativar-biometria.csv"), reativar, "reativar no CRM"),
        (origem.with_name("consultas-inativar.csv"), inativar, "inativar no CRM"),
        (origem.with_name("consultas-nao-encontrados.csv"), nao_encontrados, "nao localizados"),
    )

    print()
    for destino, pessoas, descricao in saidas:
        gravar(destino, pessoas)
        print(f"  {len(pessoas):>4}  {descricao:22} -> {destino.name}")

    if reativar:
        print(f"\nATENCAO: {len(reativar)} pessoa(s) podem ter sido inativadas pela regra antiga")
        print("de biometria e hoje deveriam estar ATIVAS. Confira no CRM.")

    print("\nEsses arquivos tem CPF e nome. Nao versione nem compartilhe fora do combinado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
