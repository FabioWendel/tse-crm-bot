from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from playwright.sync_api import Error, Page, TimeoutError, sync_playwright


TOTAL_OPERADORES = 4
PORTA_TSE_INICIAL = 9420
PER_PAGE = 100
MAX_PAGINAS = 1000
TENTATIVAS_API = 3
PAUSA_API_MS = 1000
RESUMO_A_CADA = 10
MAX_REQUISICOES_EXTERNAS_POR_MINUTO = 14
INTERVALO_API_EXTERNA_S = max(
    60.0 / MAX_REQUISICOES_EXTERNAS_POR_MINUTO,
    4.4,
)

PASTA_ATUAL = Path(__file__).resolve().parent
RAIZ = PASTA_ATUAL.parent
PASTA_DADOS = PASTA_ATUAL / "dados"
PASTA_FILA = PASTA_DADOS / "fila"
API_VALIDAR_LOCAL = "https://juniorveloso.com.br/cadastrante/api/validar-local"
RATE_LIMIT_LOCK = PASTA_DADOS / "api_rate_limit.lock"
RATE_LIMIT_STATE = PASTA_DADOS / "api_rate_limit.json"

ENV_URL = "ENRICHMENT_API_URL"
ENV_TOKEN = "ENRICHMENT_API_TOKEN"

os.environ["TSE_NAVEGADOR"] = "brave"
sys.path.insert(0, str(RAIZ))

import crm_tse_bot as bot  # noqa: E402


@dataclass
class PessoaFoto:
    crm_id: int
    cpf: str
    nome: str
    mae: str
    nascimento: str


@dataclass
class DadosConsulta:
    cpf: str
    nome: str
    mae: str
    nascimento: str


@dataclass
class Trabalho:
    foto: PessoaFoto
    consulta: DadosConsulta | None = None
    tentativas: int = 0
    ultimo_erro: str = ""


@dataclass
class Contadores:
    data_inicio: str = ""
    data_fim: str = ""
    total_inicial: int = 0
    processados: int = 0
    dados_externos_ok: int = 0
    dados_externos_falharam: int = 0
    dados_insuficientes: int = 0
    voltaram_pendentes: int = 0
    tse_consultados: int = 0
    tse_encontrados: int = 0
    tse_nao_encontrados: int = 0
    validados_confirmados: int = 0
    revisar_confirmados: int = 0
    erros_tecnicos: int = 0
    repasse_final: int = 0
    pulados_estado_mudou: int = 0
    _unicos: dict[str, set[str]] = field(default_factory=dict, repr=False)

    def unico(self, campo: str, cpf: str) -> None:
        vistos = self._unicos.setdefault(campo, set())
        if cpf in vistos:
            return
        vistos.add(cpf)
        setattr(self, campo, getattr(self, campo) + 1)

    def linha_csv(self) -> dict[str, str]:
        dados = asdict(self)
        dados.pop("_unicos", None)
        dados["taxa_encontro_tse"] = percentual(
            self.tse_encontrados,
            self.tse_consultados,
        )
        dados["taxa_validacao"] = percentual(
            self.validados_confirmados,
            self.tse_consultados,
        )
        return {chave: str(valor) for chave, valor in dados.items()}


class EstadoAba(str, Enum):
    PRESENTE = "PRESENTE"
    AUSENTE = "AUSENTE"
    INDETERMINADO = "INDETERMINADO"


@dataclass
class ConfirmacaoAba:
    estado: EstadoAba
    item: dict[str, Any] | None = None
    detalhe: str = ""


class ErroTecnico(RuntimeError):
    pass


class EstadoMudou(RuntimeError):
    pass


@contextmanager
def bloquear_arquivo_compartilhado(caminho: Path):
    """Lock multiprocesso liberado automaticamente se um processo encerrar."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("a+b") as arquivo:
        if arquivo.tell() == 0 and caminho.stat().st_size == 0:
            arquivo.write(b"0")
            arquivo.flush()

        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    arquivo.seek(0)
                    msvcrt.locking(arquivo.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX)

        try:
            yield
        finally:
            arquivo.seek(0)
            if os.name == "nt":
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(arquivo.fileno(), fcntl.LOCK_UN)


class RateLimiterGlobal:
    """Agenda requisições externas em uma linha do tempo única entre processos."""

    def __init__(self, operador: int, execucao_id: str):
        self.operador = operador
        self.execucao_id = execucao_id

    @staticmethod
    def _ler_estado() -> dict[str, Any]:
        try:
            with RATE_LIMIT_STATE.open("r", encoding="utf-8") as arquivo:
                estado = json.load(arquivo)
            if isinstance(estado, dict):
                return estado
        except (OSError, ValueError, TypeError):
            pass
        return {}

    @staticmethod
    def _salvar_estado(estado: dict[str, Any]) -> None:
        temporario = RATE_LIMIT_STATE.with_name(
            f"{RATE_LIMIT_STATE.name}.{os.getpid()}.tmp"
        )
        with temporario.open("w", encoding="utf-8") as arquivo:
            json.dump(estado, arquivo, ensure_ascii=True)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        temporario.replace(RATE_LIMIT_STATE)

    def aguardar_e_reservar(self) -> int:
        avisou = False
        while True:
            espera = 0.0
            with bloquear_arquivo_compartilhado(RATE_LIMIT_LOCK):
                estado = self._ler_estado()
                agora_epoch = time.time()
                proxima = float(estado.get("proxima_liberacao", 0.0) or 0.0)
                espera = max(proxima - agora_epoch, 0.0)
                if espera <= 0:
                    contagens = estado.get("requests_por_execucao")
                    if not isinstance(contagens, dict):
                        contagens = {}
                    total = int(contagens.get(self.execucao_id, 0) or 0) + 1
                    contagens[self.execucao_id] = total
                    # Evita crescimento ilimitado sem afetar a janela global.
                    if len(contagens) > 20:
                        contagens = dict(list(contagens.items())[-20:])
                    estado = {
                        "proxima_liberacao": agora_epoch + INTERVALO_API_EXTERNA_S,
                        "requests_por_execucao": contagens,
                    }
                    self._salvar_estado(estado)
                    print(f"[API EXTERNA] operador {self.operador} liberado")
                    return total

            if not avisou:
                print(
                    "[API EXTERNA] aguardando rate limit global... "
                    f"({espera:.1f}s)"
                )
                avisou = True
            time.sleep(min(max(espera, 0.1), 1.0))

    def adiar_global(self, segundos: float) -> None:
        atraso = max(float(segundos), INTERVALO_API_EXTERNA_S)
        with bloquear_arquivo_compartilhado(RATE_LIMIT_LOCK):
            estado = self._ler_estado()
            agora_epoch = time.time()
            atual = float(estado.get("proxima_liberacao", 0.0) or 0.0)
            estado["proxima_liberacao"] = max(atual, agora_epoch + atraso)
            if not isinstance(estado.get("requests_por_execucao"), dict):
                estado["requests_por_execucao"] = {}
            self._salvar_estado(estado)


def agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percentual(parte: int, total: int) -> str:
    return f"{(parte / total * 100) if total else 0.0:.1f}%"


def exigir_configuracao_externa() -> tuple[str, str]:
    url = os.environ.get(ENV_URL, "").strip()
    token = os.environ.get(ENV_TOKEN, "").strip()
    ausentes = [nome for nome, valor in ((ENV_URL, url), (ENV_TOKEN, token)) if not valor]
    if ausentes:
        raise RuntimeError(
            "Configuração da fonte autorizada ausente: " + ", ".join(ausentes)
        )
    return url, token


def itens_payload(payload: Any) -> tuple[list[Any], int | None]:
    if isinstance(payload, list):
        return payload, None
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ErroTecnico("API do CRM devolveu JSON sem lista de itens.")
    total = None
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("total"), int):
        total = meta["total"]
    elif isinstance(payload.get("total"), int):
        total = payload["total"]
    return payload["items"], total


def requisitar_json(
    page: Page,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30000,
) -> Any:
    resposta = None
    try:
        resposta = page.request.get(
            url,
            params=params,
            headers=headers or {"Accept": "application/json"},
            timeout=timeout,
        )
        if resposta.status != 200:
            raise ErroTecnico(f"requisição respondeu HTTP {resposta.status}")
        try:
            return resposta.json()
        except Exception as exc:
            raise ErroTecnico("requisição devolveu JSON inválido") from exc
    finally:
        if resposta is not None:
            resposta.dispose()


def fotografar_nao_encontrados(page: Page) -> tuple[list[PessoaFoto], int | None, int]:
    por_id: dict[int, PessoaFoto] = {}
    cpfs_vistos: set[str] = set()
    ids_pagina_anterior: tuple[int, ...] | None = None
    total_informado: int | None = None
    itens_recebidos = 0

    for numero_pagina in range(1, MAX_PAGINAS + 1):
        ultimo_erro = ""
        items: list[Any] | None = None
        for tentativa in range(1, TENTATIVAS_API + 1):
            try:
                payload = requisitar_json(
                    page,
                    API_VALIDAR_LOCAL,
                    params={
                        "aba": "nao_encontrados",
                        "q": "",
                        "page": numero_pagina,
                        "per_page": PER_PAGE,
                        "sort": "nome",
                        "dir": "asc",
                    },
                )
                items, total_pagina = itens_payload(payload)
                if total_pagina is not None:
                    if total_informado is not None and total_informado != total_pagina:
                        raise ErroTecnico("total da API mudou durante a fotografia")
                    total_informado = total_pagina
                break
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                ultimo_erro = bot.resumo_erro(exc)
                if tentativa < TENTATIVAS_API:
                    page.wait_for_timeout(PAUSA_API_MS)
        if items is None:
            raise ErroTecnico(
                f"não foi possível fotografar a página {numero_pagina}: {ultimo_erro}"
            )

        itens_recebidos += len(items)
        ids_pagina: list[int] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                crm_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if crm_id <= 0:
                continue
            ids_pagina.append(crm_id)
            cpf = bot.only_digits(str(item.get("cpf") or ""))
            if crm_id in por_id or (cpf and cpf in cpfs_vistos):
                continue
            por_id[crm_id] = PessoaFoto(
                crm_id=crm_id,
                cpf=cpf,
                nome=bot.clean_person_name(str(item.get("nome") or "")),
                mae=bot.clean(str(item.get("nome_da_mae") or "")),
                nascimento=bot.normalizar_nascimento_api(item.get("nascimento")),
            )
            if cpf:
                cpfs_vistos.add(cpf)

        assinatura = tuple(ids_pagina)
        if ids_pagina_anterior is not None and assinatura == ids_pagina_anterior:
            raise ErroTecnico("a API repetiu exatamente os IDs da página anterior")
        ids_pagina_anterior = assinatura
        print(
            f"API Não encontrados: página {numero_pagina}, "
            f"+{len(items)}, total fotografado {len(por_id)}"
        )
        if len(items) < PER_PAGE:
            break
    else:
        raise ErroTecnico("limite de segurança de 1000 páginas atingido")

    if total_informado is not None and itens_recebidos != total_informado:
        raise ErroTecnico(
            f"fotografia incompleta: API informou {total_informado}, "
            f"mas retornou {itens_recebidos} itens"
        )
    return list(por_id.values()), total_informado, itens_recebidos


def salvar_fotografia(pessoas: list[PessoaFoto]) -> Path:
    PASTA_FILA.mkdir(parents=True, exist_ok=True)
    caminho = PASTA_FILA / f"fila-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    temporario = caminho.with_suffix(".tmp")
    with temporario.open("w", encoding="utf-8-sig", newline="") as arquivo:
        writer = csv.DictWriter(
            arquivo,
            fieldnames=["id", "cpf", "nome", "mae", "nascimento"],
        )
        writer.writeheader()
        for pessoa in pessoas:
            writer.writerow({
                "id": pessoa.crm_id,
                "cpf": pessoa.cpf,
                "nome": pessoa.nome,
                "mae": pessoa.mae,
                "nascimento": pessoa.nascimento,
            })
    temporario.replace(caminho)
    return caminho


def carregar_fotografia(caminho: Path) -> list[PessoaFoto]:
    pessoas: list[PessoaFoto] = []
    with caminho.open("r", encoding="utf-8-sig", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            try:
                crm_id = int(linha.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if crm_id <= 0:
                continue
            pessoas.append(PessoaFoto(
                crm_id=crm_id,
                cpf=bot.only_digits(str(linha.get("cpf") or "")),
                nome=str(linha.get("nome") or "").strip(),
                mae=str(linha.get("mae") or "").strip(),
                nascimento=str(linha.get("nascimento") or "").strip(),
            ))
    return pessoas


def confirmar_em_aba(page: Page, aba: str, cpf: str) -> ConfirmacaoAba:
    cpf = bot.only_digits(cpf)
    if len(cpf) != 11:
        return ConfirmacaoAba(EstadoAba.INDETERMINADO, detalhe="CPF inválido")
    houve_erro = False
    erros: list[str] = []
    for tentativa in range(1, TENTATIVAS_API + 1):
        try:
            payload = requisitar_json(
                page,
                API_VALIDAR_LOCAL,
                params={
                    "aba": aba,
                    "q": cpf,
                    "page": 1,
                    "per_page": 25,
                    "sort": "nome",
                    "dir": "asc",
                },
            )
            items, _ = itens_payload(payload)
            if any(not isinstance(item, dict) for item in items):
                raise ErroTecnico("API devolveu item em formato inválido")
            for item in items:
                if bot.only_digits(str(item.get("cpf") or "")) == cpf:
                    return ConfirmacaoAba(
                        EstadoAba.PRESENTE,
                        item=item,
                        detalhe=f"CPF encontrado na tentativa {tentativa}",
                    )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            houve_erro = True
            erros.append(f"tentativa {tentativa}: {bot.resumo_erro(exc)}")
        if tentativa < TENTATIVAS_API:
            page.wait_for_timeout(PAUSA_API_MS)
    if houve_erro:
        return ConfirmacaoAba(EstadoAba.INDETERMINADO, detalhe="; ".join(erros))
    return ConfirmacaoAba(
        EstadoAba.AUSENTE,
        detalhe="três respostas válidas não contiveram o CPF",
    )


def selecionar_registro_externo(payload: Any, cpf: str) -> dict[str, Any]:
    candidatos: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        candidatos.append(payload)
        for chave in ("data", "item", "result"):
            valor = payload.get(chave)
            if isinstance(valor, dict):
                candidatos.append(valor)
        for chave in ("items", "results", "data"):
            valor = payload.get(chave)
            if isinstance(valor, list):
                candidatos.extend(item for item in valor if isinstance(item, dict))
    elif isinstance(payload, list):
        candidatos.extend(item for item in payload if isinstance(item, dict))

    for candidato in candidatos:
        cpf_item = bot.only_digits(str(
            candidato.get("cpf")
            or candidato.get("documento")
            or candidato.get("document")
            or ""
        ))
        if cpf_item == cpf:
            return candidato
    raise ErroTecnico("fonte autorizada não devolveu o CPF solicitado")


def resposta_explicita_sem_dados(payload: Any) -> bool:
    """Aceita apenas marcadores inequívocos; formato inesperado continua técnico."""
    candidatos = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        candidatos.append(payload["data"])
    for candidato in candidatos:
        if not isinstance(candidato, dict):
            continue
        for chave in ("found", "encontrado", "exists", "existe"):
            if candidato.get(chave) is False:
                return True
        status = bot.normalize_text(str(candidato.get("status") or ""))
        if status in {"NAO ENCONTRADO", "NOT FOUND", "SEM DADOS", "NO DATA"}:
            return True
    return False


def segundos_retry_after(headers: dict[str, str]) -> float:
    valor = str(headers.get("retry-after") or headers.get("Retry-After") or "").strip()
    if not valor:
        return INTERVALO_API_EXTERNA_S
    try:
        return max(float(valor), INTERVALO_API_EXTERNA_S)
    except ValueError:
        try:
            instante = parsedate_to_datetime(valor)
            return max(instante.timestamp() - time.time(), INTERVALO_API_EXTERNA_S)
        except (TypeError, ValueError, OverflowError):
            return INTERVALO_API_EXTERNA_S


def primeiro_valor(item: dict[str, Any], nomes: tuple[str, ...]) -> str:
    for nome in nomes:
        valor = item.get(nome)
        if valor is not None:
            return bot.clean(str(valor))
    return ""


def obter_dados_autorizados(
    page: Page,
    cpf: str,
    url_base: str,
    token: str,
    rate_limiter: RateLimiterGlobal,
) -> DadosConsulta:
    cpf = bot.only_digits(cpf)
    url = url_base.replace("{cpf}", quote(cpf, safe=""))
    params = None if "{cpf}" in url_base else {"cpf": cpf}
    ultimo_erro = "fonte autorizada indisponível ou resposta inválida"

    for tentativa in range(1, 4):
        resposta = None
        total_execucao = rate_limiter.aguardar_e_reservar()
        print(f"[API EXTERNA] requests desta execução: {total_execucao}")
        try:
            resposta = page.request.get(
                url,
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=45000,
            )
            print(f"[API EXTERNA] CPF {cpf} consultado")

            if resposta.status == 429:
                espera = segundos_retry_after(resposta.headers)
                rate_limiter.adiar_global(espera)
                ultimo_erro = f"HTTP 429; nova tentativa após {espera:.1f}s"
                if tentativa < 3:
                    print(f"[API EXTERNA] {ultimo_erro}")
                    continue
                break

            payload = None
            try:
                payload = resposta.json()
            except Exception:
                if resposta.status == 200:
                    ultimo_erro = "HTTP 200 com JSON inválido"
                    if tentativa < 3:
                        continue
                    break

            if resposta.status != 200:
                if resposta.status == 404 and resposta_explicita_sem_dados(payload):
                    return DadosConsulta(cpf=cpf, nome="", mae="", nascimento="")
                ultimo_erro = f"HTTP {resposta.status} da fonte autorizada"
                if resposta.status >= 500 and tentativa < 3:
                    continue
                break

            if resposta_explicita_sem_dados(payload):
                return DadosConsulta(cpf=cpf, nome="", mae="", nascimento="")

            try:
                item = selecionar_registro_externo(payload, cpf)
            except ErroTecnico:
                ultimo_erro = "resposta válida, mas sem registro correspondente ao CPF"
                if tentativa < 3:
                    continue
                break

            # Somente estes quatro valores deixam esta função. O JSON bruto não
            # é impresso, salvo nem devolvido ao restante do programa.
            return DadosConsulta(
                cpf=cpf,
                nome=primeiro_valor(
                    item,
                    ("nome", "name", "nome_completo", "full_name"),
                ),
                mae=primeiro_valor(
                    item,
                    ("nome_mae", "nome_da_mae", "mae", "mother_name"),
                ),
                nascimento=bot.normalizar_nascimento_api(primeiro_valor(
                    item,
                    ("nascimento", "data_nascimento", "birth_date", "date_of_birth"),
                )),
            )
        except KeyboardInterrupt:
            raise
        except Exception:
            # Não propaga URL/cabeçalhos da biblioteca para logs ou traceback.
            ultimo_erro = "timeout, rede interrompida ou falha na fonte autorizada"
            if tentativa < 3:
                continue
        finally:
            if resposta is not None:
                try:
                    resposta.dispose()
                except Exception:
                    pass

    raise ErroTecnico(ultimo_erro) from None


def validar_dados(dados: DadosConsulta) -> tuple[bool, str]:
    ausentes: list[str] = []
    if len(bot.only_digits(dados.cpf)) != 11:
        ausentes.append("CPF")
    if bot.campo_sem_informacao(dados.nome):
        ausentes.append("nome")
    if bot.campo_sem_informacao(dados.mae):
        ausentes.append("nome da mãe")
    if not dados.nascimento or bot.campo_sem_informacao(dados.nascimento):
        ausentes.append("nascimento")
    return not ausentes, ", ".join(ausentes)


def cabecalhos_csrf(page: Page) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    try:
        token_meta = page.locator('meta[name="csrf-token"]').first.get_attribute(
            "content",
            timeout=1000,
        )
    except (TimeoutError, Error):
        token_meta = None
    if token_meta:
        headers["X-CSRF-TOKEN"] = token_meta
        return headers
    for cookie in page.context.cookies():
        if cookie.get("name") == "XSRF-TOKEN" and cookie.get("value"):
            headers["X-XSRF-TOKEN"] = unquote(str(cookie["value"]))
            break
    return headers


def postar_acao(page: Page, url: str, body: dict[str, Any]) -> dict[str, Any]:
    resposta = None
    try:
        resposta = page.request.post(
            url,
            data=body,
            headers=cabecalhos_csrf(page),
            timeout=30000,
        )
        if resposta.status != 200:
            raise ErroTecnico(f"ação do CRM respondeu HTTP {resposta.status}")
        try:
            payload = resposta.json()
        except Exception as exc:
            raise ErroTecnico("ação do CRM devolveu JSON inválido") from exc
        if not isinstance(payload, dict):
            raise ErroTecnico("ação do CRM devolveu formato inválido")
        return payload
    finally:
        if resposta is not None:
            resposta.dispose()


def voltar_para_pendentes_api(page: Page, trabalho: Trabalho) -> None:
    estado = confirmar_em_aba(page, "nao_encontrados", trabalho.foto.cpf)
    if estado.estado == EstadoAba.AUSENTE:
        raise EstadoMudou("CPF não está mais em Não encontrados")
    if estado.estado == EstadoAba.INDETERMINADO:
        raise ErroTecnico("estado em Não encontrados indeterminado: " + estado.detalhe)
    payload = postar_acao(
        page,
        f"{API_VALIDAR_LOCAL}/{trabalho.foto.crm_id}/nao-encontrado",
        {"desfazer": True},
    )
    if payload.get("ok") is not True or payload.get("marcado") is not False:
        raise ErroTecnico("CRM não confirmou a reversão de Não encontrado")
    confirmacao = bot.confirmar_pendente_por_api(page, trabalho.foto.cpf)
    if confirmacao.estado != bot.EstadoPendente.PENDENTE:
        raise ErroTecnico(
            "não foi possível confirmar o retorno para Pendentes: "
            + confirmacao.detalhe
        )


def marcar_revisar_api(page: Page, pessoa: bot.Pessoa) -> None:
    confirmar_antes_de_gravar(page, pessoa.cpf)
    if pessoa.crm_id is None:
        raise ErroTecnico("ID do CRM ausente para marcar Revisar")
    payload = postar_acao(
        page,
        f"{API_VALIDAR_LOCAL}/{pessoa.crm_id}/revisar",
        {"desfazer": False},
    )
    if payload.get("ok") is not True or payload.get("marcado") is not True:
        raise ErroTecnico("CRM não confirmou a marcação para Revisar")
    if not bot.confirmar_saida_de_pendentes(page, pessoa.cpf):
        raise ErroTecnico("API não confirmou a saída de Pendentes após Revisar")


def confirmar_antes_de_gravar(page: Page, cpf: str) -> bot.Pessoa:
    confirmacao = bot.confirmar_pendente_por_api(page, cpf)
    if confirmacao.estado == bot.EstadoPendente.FORA_DE_PENDENTES:
        raise EstadoMudou("CPF saiu de Pendentes enquanto era processado")
    if confirmacao.estado == bot.EstadoPendente.INDETERMINADO:
        raise ErroTecnico("estado em Pendentes indeterminado: " + confirmacao.detalhe)
    if confirmacao.pessoa is None:
        raise ErroTecnico("API confirmou Pendentes sem devolver a pessoa")
    return confirmacao.pessoa


def pessoa_consulta(trabalho: Trabalho) -> bot.Pessoa:
    if trabalho.consulta is None:
        raise ErroTecnico("dados autorizados não foram carregados")
    return bot.Pessoa(
        row_index=-1,
        nome=trabalho.consulta.nome,
        cpf=trabalho.consulta.cpf,
        mae=trabalho.consulta.mae,
        nascimento=trabalho.consulta.nascimento,
        crm_id=trabalho.foto.crm_id,
    )


def configurar_operador(operador: int) -> Path:
    dados = PASTA_DADOS / f"operador-{operador}"
    dados.mkdir(parents=True, exist_ok=True)
    bot.BASE_DIR = dados
    bot.PROFILE_DIR = dados / "perfil-crm"
    bot.TSE_PROFILE_DIR = dados / "perfil-tse-brave"
    bot.LOG_FILE = dados / "consultas.csv"
    bot.ERROR_LOG = dados / "bot_error.log"
    bot.DETAIL_LOG = dados / "execucao_detalhada.txt"
    bot.TSE_NAVEGADOR = "brave"
    bot.TSE_REMOTE_DEBUGGING_PORT = PORTA_TSE_INICIAL + operador
    return dados


def escrever_csv(caminho: Path, linhas: list[dict[str, Any]], campos: list[str]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    with temporario.open("w", encoding="utf-8-sig", newline="") as arquivo:
        writer = csv.DictWriter(arquivo, fieldnames=campos, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(linhas)
    temporario.replace(caminho)


def ler_csv(caminho: Path) -> list[dict[str, str]]:
    if not caminho.exists():
        return []
    with caminho.open("r", encoding="utf-8-sig", newline="") as arquivo:
        return list(csv.DictReader(arquivo))


REPASSE_CAMPOS = [
    "id", "cpf", "nome", "mae", "nascimento", "nome_crm", "mae_crm",
    "nascimento_crm", "tentativas", "ultimo_erro",
]
RESULTADO_CAMPOS = ["id", "cpf", "resultado_final", "origem_dados", "tentativa", "data_hora"]
DIVERGENCIA_CAMPOS = [
    "id", "cpf", "nome_crm", "nome_consulta", "mae_crm", "mae_consulta",
    "nascimento_crm", "nascimento_consulta",
]
INCOMPLETOS_CAMPOS = ["id", "cpf", "nome", "mae", "nascimento", "motivo", "data_hora"]
RESUMO_CAMPOS = [
    "data_inicio", "data_fim", "total_inicial", "processados",
    "dados_externos_ok", "dados_externos_falharam", "dados_insuficientes",
    "voltaram_pendentes", "tse_consultados", "tse_encontrados",
    "tse_nao_encontrados", "validados_confirmados", "revisar_confirmados",
    "erros_tecnicos", "repasse_final", "pulados_estado_mudou",
    "taxa_encontro_tse", "taxa_validacao",
]


def trabalho_de_repasse(linha: dict[str, str]) -> Trabalho | None:
    try:
        crm_id = int(linha.get("id") or 0)
    except (TypeError, ValueError):
        return None
    cpf = bot.only_digits(str(linha.get("cpf") or ""))
    if crm_id <= 0 or len(cpf) != 11:
        return None
    consulta = DadosConsulta(
        cpf=cpf,
        nome=str(linha.get("nome") or "").strip(),
        mae=str(linha.get("mae") or "").strip(),
        nascimento=bot.normalizar_nascimento_api(linha.get("nascimento")),
    )
    if not any((consulta.nome, consulta.mae, consulta.nascimento)):
        consulta = None
    return Trabalho(
        foto=PessoaFoto(
            crm_id=crm_id,
            cpf=cpf,
            nome=str(linha.get("nome_crm") or "").strip(),
            mae=str(linha.get("mae_crm") or "").strip(),
            nascimento=bot.normalizar_nascimento_api(linha.get("nascimento_crm")),
        ),
        consulta=consulta,
        tentativas=int(linha.get("tentativas") or 0),
        ultimo_erro=str(linha.get("ultimo_erro") or ""),
    )


def linha_repasse(trabalho: Trabalho) -> dict[str, Any]:
    consulta = trabalho.consulta or DadosConsulta(trabalho.foto.cpf, "", "", "")
    return {
        "id": trabalho.foto.crm_id,
        "cpf": trabalho.foto.cpf,
        "nome": consulta.nome,
        "mae": consulta.mae,
        "nascimento": consulta.nascimento,
        "nome_crm": trabalho.foto.nome,
        "mae_crm": trabalho.foto.mae,
        "nascimento_crm": trabalho.foto.nascimento,
        "tentativas": trabalho.tentativas,
        "ultimo_erro": trabalho.ultimo_erro,
    }


def salvar_repasse_operador(
    dados: Path,
    trabalhos: dict[str, Trabalho],
) -> None:
    escrever_csv(
        dados / "repasse.csv",
        [linha_repasse(item) for item in trabalhos.values()],
        REPASSE_CAMPOS,
    )


def registrar_resultado(
    resultados: list[dict[str, Any]],
    trabalho: Trabalho,
    resultado: str,
    origem: str,
) -> None:
    resultados.append({
        "id": trabalho.foto.crm_id,
        "cpf": trabalho.foto.cpf,
        "resultado_final": resultado,
        "origem_dados": origem,
        "tentativa": trabalho.tentativas,
        "data_hora": agora(),
    })


def registrar_divergencia(
    divergencias: dict[str, dict[str, Any]],
    trabalho: Trabalho,
) -> None:
    dados = trabalho.consulta
    if dados is None:
        return
    crm = trabalho.foto
    diferentes = any((
        bot.normalize_text(crm.nome) != bot.normalize_text(dados.nome),
        bot.normalize_text(crm.mae) != bot.normalize_text(dados.mae),
        crm.nascimento != dados.nascimento,
    ))
    if diferentes:
        divergencias[crm.cpf] = {
            "id": crm.crm_id,
            "cpf": crm.cpf,
            "nome_crm": crm.nome,
            "nome_consulta": dados.nome,
            "mae_crm": crm.mae,
            "mae_consulta": dados.mae,
            "nascimento_crm": crm.nascimento,
            "nascimento_consulta": dados.nascimento,
        }


def estado_inicial_repasse(page: Page, trabalho: Trabalho) -> str:
    pendente = bot.confirmar_pendente_por_api(page, trabalho.foto.cpf)
    if pendente.estado == bot.EstadoPendente.PENDENTE:
        return "PENDENTE"
    if pendente.estado == bot.EstadoPendente.INDETERMINADO:
        raise ErroTecnico("estado em Pendentes indeterminado: " + pendente.detalhe)
    nao_encontrado = confirmar_em_aba(page, "nao_encontrados", trabalho.foto.cpf)
    if nao_encontrado.estado == EstadoAba.PRESENTE:
        return "NAO_ENCONTRADO"
    if nao_encontrado.estado == EstadoAba.INDETERMINADO:
        raise ErroTecnico(
            "estado em Não encontrados indeterminado: " + nao_encontrado.detalhe
        )
    raise EstadoMudou("CPF não está em Pendentes nem em Não encontrados")


def processar_trabalho(
    playwright,
    context,
    crm: Page,
    trabalho: Trabalho,
    url_externa: str,
    token: str,
    rate_limiter: RateLimiterGlobal,
    contadores: Contadores,
    resultados: list[dict[str, Any]],
    divergencias: dict[str, dict[str, Any]],
    incompletos: dict[str, dict[str, Any]],
    *,
    vindo_repasse: bool,
) -> bool:
    trabalho.tentativas += 1
    cpf = trabalho.foto.cpf
    pessoa_erro = bot.Pessoa(
        row_index=-1,
        nome=trabalho.foto.nome,
        cpf=cpf,
        mae=trabalho.foto.mae,
        nascimento=trabalho.foto.nascimento,
        crm_id=trabalho.foto.crm_id,
    )
    origem = "fonte_autorizada" if trabalho.consulta else "não_obtida"

    try:
        if len(cpf) != 11:
            motivo = "CPF deve conter exatamente 11 dígitos"
            contadores.unico("dados_insuficientes", cpf or f"id:{trabalho.foto.crm_id}")
            incompletos[cpf or f"id:{trabalho.foto.crm_id}"] = {
                "id": trabalho.foto.crm_id,
                "cpf": cpf,
                "nome": trabalho.foto.nome,
                "mae": trabalho.foto.mae,
                "nascimento": trabalho.foto.nascimento,
                "motivo": motivo,
                "data_hora": agora(),
            }
            registrar_resultado(resultados, trabalho, "DADOS_INSUFICIENTES", origem)
            return True

        if vindo_repasse:
            estado = estado_inicial_repasse(crm, trabalho)
        else:
            estado_nao_encontrado = confirmar_em_aba(crm, "nao_encontrados", cpf)
            if estado_nao_encontrado.estado == EstadoAba.AUSENTE:
                raise EstadoMudou("CPF saiu de Não encontrados após a fotografia")
            if estado_nao_encontrado.estado == EstadoAba.INDETERMINADO:
                raise ErroTecnico(
                    "estado em Não encontrados indeterminado: "
                    + estado_nao_encontrado.detalhe
                )
            estado = "NAO_ENCONTRADO"

        if trabalho.consulta is None:
            try:
                trabalho.consulta = obter_dados_autorizados(
                    crm,
                    cpf,
                    url_externa,
                    token,
                    rate_limiter,
                )
                origem = "fonte_autorizada"
                contadores.unico("dados_externos_ok", cpf)
            except Exception:
                contadores.unico("dados_externos_falharam", cpf)
                raise

        registrar_divergencia(divergencias, trabalho)
        valido, motivo = validar_dados(trabalho.consulta)
        if not valido:
            contadores.unico("dados_insuficientes", cpf)
            incompletos[cpf] = {
                "id": trabalho.foto.crm_id,
                "cpf": cpf,
                "nome": trabalho.consulta.nome,
                "mae": trabalho.consulta.mae,
                "nascimento": trabalho.consulta.nascimento,
                "motivo": motivo,
                "data_hora": agora(),
            }
            registrar_resultado(resultados, trabalho, "DADOS_INSUFICIENTES", origem)
            return True

        if estado == "NAO_ENCONTRADO":
            voltar_para_pendentes_api(crm, trabalho)
            contadores.unico("voltaram_pendentes", cpf)

        confirmar_antes_de_gravar(crm, cpf)
        pessoa = pessoa_consulta(trabalho)
        resultado_tse = None
        try:
            resultado_tse = bot.consultar_tse(playwright, context, pessoa)
            bot.append_log(pessoa, resultado_tse)

            if resultado_tse.resposta_do_tse is not True:
                raise ErroTecnico("consulta do TSE não produziu resposta conclusiva")
            contadores.tse_consultados += 1

            if resultado_tse.encontrado is True:
                contadores.tse_encontrados += 1
                confirmar_antes_de_gravar(crm, cpf)
                if not bot.atualizar_crm(crm, pessoa, resultado_tse.texto_para_crm):
                    raise ErroTecnico("salvamento do local não foi confirmado no CRM")
                contadores.unico("validados_confirmados", cpf)
                motivo_inativacao = bot.motivo_inativacao(resultado_tse)
                if motivo_inativacao:
                    try:
                        bot.inativar_cadastro_validado(crm, pessoa, motivo_inativacao)
                    except Exception as exc:
                        bot.registrar_erro(pessoa, 0, exc)
                registrar_resultado(resultados, trabalho, "VALIDADO", origem)
                return True

            if resultado_tse.encontrado is False:
                contadores.tse_nao_encontrados += 1
                marcar_revisar_api(crm, pessoa)
                contadores.unico("revisar_confirmados", cpf)
                registrar_resultado(resultados, trabalho, "REVISAR", origem)
                return True

            raise ErroTecnico("resultado do TSE indeterminado")
        finally:
            if resultado_tse is not None:
                bot.fechar_tse_resultado(resultado_tse)
            try:
                bot.voltar_para_pendentes(crm)
            except Exception:
                pass
    except EstadoMudou as exc:
        contadores.unico("pulados_estado_mudou", cpf)
        trabalho.ultimo_erro = str(exc)
        registrar_resultado(resultados, trabalho, "PULADO_ESTADO_MUDOU", origem)
        return True
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        trabalho.ultimo_erro = bot.resumo_erro(exc)
        contadores.unico("erros_tecnicos", cpf)
        bot.registrar_erro(pessoa_erro, trabalho.tentativas, exc)
        registrar_resultado(
            resultados,
            trabalho,
            "REPASSE" if vindo_repasse else "ERRO_TECNICO",
            origem,
        )
        return False


def mostrar_progresso(contadores: Contadores) -> None:
    restantes = max(contadores.total_inicial - contadores.processados, 0)
    print("=" * 72)
    print("REVALIDAÇÃO DE NÃO ENCONTRADOS")
    print("=" * 72)
    print(f"Processados:                         {contadores.processados} / {contadores.total_inicial}")
    print(f"Restantes:                           {restantes}")
    print(f"Dados externos obtidos:              {contadores.dados_externos_ok}")
    print(f"Falhas nos dados externos:           {contadores.dados_externos_falharam}")
    print(f"Dados insuficientes:                 {contadores.dados_insuficientes}")
    print(f"Voltaram para Pendentes:             {contadores.voltaram_pendentes}")
    print(f"TSE realmente consultados:           {contadores.tse_consultados}")
    print(f"TSE encontrou:                       {contadores.tse_encontrados}")
    print(f"TSE não encontrou realmente:         {contadores.tse_nao_encontrados}")
    print(f"Validados confirmados no CRM:        {contadores.validados_confirmados}")
    print(f"Enviados para Revisar:               {contadores.revisar_confirmados}")
    print(f"Erros técnicos:                      {contadores.erros_tecnicos}")
    print(f"Ainda no repasse:                    {contadores.repasse_final}")
    print(f"Pulados por mudança de estado:       {contadores.pulados_estado_mudou}")
    print(f"Taxa de validação:                   {percentual(contadores.validados_confirmados, contadores.tse_consultados)}")
    print("=" * 72)


def executar_operador(
    operador: int,
    caminho_fila: Path,
    execucao_id: str,
) -> int:
    url_externa, token = exigir_configuracao_externa()
    rate_limiter = RateLimiterGlobal(operador, execucao_id)
    dados = configurar_operador(operador)
    fila = [
        Trabalho(pessoa)
        for pessoa in carregar_fotografia(caminho_fila)
        if pessoa.crm_id % TOTAL_OPERADORES == operador
    ]
    repasse_anterior: dict[str, Trabalho] = {}
    for linha in ler_csv(dados / "repasse.csv"):
        trabalho = trabalho_de_repasse(linha)
        if trabalho is not None and trabalho.foto.crm_id % TOTAL_OPERADORES == operador:
            repasse_anterior[trabalho.foto.cpf] = trabalho
    fila = [item for item in fila if item.foto.cpf not in repasse_anterior]
    trabalhos_iniciais = list(repasse_anterior.values()) + fila

    contadores = Contadores(
        data_inicio=agora(),
        total_inicial=len(trabalhos_iniciais),
    )
    resultados: list[dict[str, Any]] = []
    divergencias: dict[str, dict[str, Any]] = {}
    incompletos: dict[str, dict[str, Any]] = {}
    repasse_persistido: dict[str, Trabalho] = dict(repasse_anterior)
    context = None

    print(f"Operador {operador}: {len(fila)} novo(s), {len(repasse_anterior)} repasse(s).")
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(bot.PROFILE_DIR),
                executable_path=bot.find_chrome_executable(),
                headless=bot.HEADLESS,
                slow_mo=bot.SLOW_MO_MS,
                viewport={"width": 1366, "height": 768},
                args=["--start-maximized"],
            )
            crm = context.pages[0] if context.pages else context.new_page()
            if not bot.ir_para(crm, bot.CRM_URL):
                raise ErroTecnico("não foi possível abrir o CRM")
            bot.ensure_crm_ready(crm)

            # A fila principal não inclui o repasse antigo. Erros desta etapa e
            # repasses persistentes recebem exatamente uma tentativa ao final.
            for indice, trabalho in enumerate(fila, start=1):
                print(f"[{indice}/{len(fila)}] CPF {trabalho.foto.cpf}")
                # O CPF entra preventivamente no repasse. Só sai depois de uma
                # decisão confirmada, protegendo interrupções após voltar a Pendentes.
                repasse_persistido[trabalho.foto.cpf] = trabalho
                salvar_repasse_operador(dados, repasse_persistido)
                sucesso = processar_trabalho(
                    playwright, context, crm, trabalho, url_externa, token,
                    rate_limiter,
                    contadores, resultados, divergencias, incompletos,
                    vindo_repasse=False,
                )
                contadores.processados += 1
                if sucesso:
                    repasse_persistido.pop(trabalho.foto.cpf, None)
                salvar_repasse_operador(dados, repasse_persistido)
                if indice % RESUMO_A_CADA == 0:
                    mostrar_progresso(contadores)

            itens_repasse = list(repasse_persistido.values())
            if itens_repasse:
                print(f"Iniciando uma rodada de repasse: {len(itens_repasse)} CPF(s).")
            for trabalho in itens_repasse:
                sucesso = processar_trabalho(
                    playwright, context, crm, trabalho, url_externa, token,
                    rate_limiter,
                    contadores, resultados, divergencias, incompletos,
                    vindo_repasse=True,
                )
                if trabalho.foto.cpf in repasse_anterior:
                    contadores.processados += 1
                if sucesso:
                    repasse_persistido.pop(trabalho.foto.cpf, None)
                else:
                    repasse_persistido[trabalho.foto.cpf] = trabalho
                salvar_repasse_operador(dados, repasse_persistido)
            contadores.repasse_final = len(repasse_persistido)
    except KeyboardInterrupt:
        print("Interrompido. O repasse conhecido será preservado.")
        return 130
    except Exception as exc:
        with bot.ERROR_LOG.open("a", encoding="utf-8") as arquivo:
            arquivo.write(f"\n{'=' * 70}\n{agora()} | erro geral do operador\n")
            arquivo.write(traceback.format_exc())
        print(f"Operador {operador} parou com erro: {bot.resumo_erro(exc)}")
        return 1
    finally:
        bot.encerrar_sessao_tse()
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        contadores.repasse_final = len(repasse_persistido)
        salvar_repasse_operador(dados, repasse_persistido)
        contadores.data_fim = agora()
        escrever_csv(dados / "resultado.csv", resultados, RESULTADO_CAMPOS)
        escrever_csv(dados / "divergencias.csv", list(divergencias.values()), DIVERGENCIA_CAMPOS)
        escrever_csv(dados / "dados_incompletos.csv", list(incompletos.values()), INCOMPLETOS_CAMPOS)
        escrever_csv(dados / "resumo_execucao.csv", [contadores.linha_csv()], RESUMO_CAMPOS)
        mostrar_progresso(contadores)
    return 0


def consolidar(inicio_execucao: str) -> None:
    resultados: list[dict[str, str]] = []
    divergencias: list[dict[str, str]] = []
    incompletos: list[dict[str, str]] = []
    repasses: list[dict[str, str]] = []
    resumos: list[dict[str, str]] = []
    for operador in range(TOTAL_OPERADORES):
        dados = PASTA_DADOS / f"operador-{operador}"
        resumo_operador = ler_csv(dados / "resumo_execucao.csv")
        if not resumo_operador or str(
            resumo_operador[0].get("data_inicio") or ""
        ) < inicio_execucao:
            continue
        resultados.extend(ler_csv(dados / "resultado.csv"))
        divergencias.extend(ler_csv(dados / "divergencias.csv"))
        incompletos.extend(ler_csv(dados / "dados_incompletos.csv"))
        repasses.extend(ler_csv(dados / "repasse.csv"))
        resumos.extend(resumo_operador)
    escrever_csv(PASTA_DADOS / "resultado.csv", resultados, RESULTADO_CAMPOS)
    escrever_csv(PASTA_DADOS / "divergencias.csv", divergencias, DIVERGENCIA_CAMPOS)
    escrever_csv(PASTA_DADOS / "dados_incompletos.csv", incompletos, INCOMPLETOS_CAMPOS)
    escrever_csv(PASTA_DADOS / "repasse.csv", repasses, REPASSE_CAMPOS)

    somar = [campo for campo in RESUMO_CAMPOS if campo not in {
        "data_inicio", "data_fim", "taxa_encontro_tse", "taxa_validacao"
    }]
    resumo = {campo: 0 for campo in somar}
    for linha in resumos:
        for campo in somar:
            try:
                resumo[campo] += int(linha.get(campo) or 0)
            except ValueError:
                pass
    inicio = min((linha.get("data_inicio", "") for linha in resumos if linha.get("data_inicio")), default="")
    fim = max((linha.get("data_fim", "") for linha in resumos if linha.get("data_fim")), default="")
    consolidado = {
        "data_inicio": inicio,
        "data_fim": fim,
        **{campo: str(valor) for campo, valor in resumo.items()},
        "taxa_encontro_tse": percentual(resumo["tse_encontrados"], resumo["tse_consultados"]),
        "taxa_validacao": percentual(resumo["validados_confirmados"], resumo["tse_consultados"]),
    }
    escrever_csv(PASTA_DADOS / "resumo_execucao.csv", [consolidado], RESUMO_CAMPOS)
    print("=" * 72)
    print("RESULTADO FINAL - REVALIDAÇÃO DE NÃO ENCONTRADOS")
    print("=" * 72)
    rotulos = (
        ("Total inicial", "total_inicial"),
        ("Processados", "processados"),
        ("Dados externos obtidos corretamente", "dados_externos_ok"),
        ("Falhas na obtenção de dados", "dados_externos_falharam"),
        ("Dados insuficientes", "dados_insuficientes"),
        ("Voltaram para Pendentes", "voltaram_pendentes"),
        ("TSE realmente consultados", "tse_consultados"),
        ("TSE encontrou local", "tse_encontrados"),
        ("TSE realmente não encontrou", "tse_nao_encontrados"),
        ("Validados confirmados no CRM", "validados_confirmados"),
        ("Confirmados em Revisar", "revisar_confirmados"),
        ("Erros técnicos", "erros_tecnicos"),
        ("Ainda no repasse", "repasse_final"),
        ("Pulados por mudança de estado", "pulados_estado_mudou"),
        ("Taxa de encontro no TSE", "taxa_encontro_tse"),
        ("Taxa de validação confirmada", "taxa_validacao"),
    )
    for rotulo, campo in rotulos:
        print(f"{rotulo + ':':40} {consolidado.get(campo, '')}")
    print("=" * 72)


def executar_coordenador() -> int:
    exigir_configuracao_externa()
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)
    perfil = PASTA_DADOS / "perfil-crm-coordenador"
    context = None
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(perfil),
                executable_path=bot.find_chrome_executable(),
                headless=bot.HEADLESS,
                slow_mo=bot.SLOW_MO_MS,
                viewport={"width": 1366, "height": 768},
                args=["--start-maximized"],
            )
            crm = context.pages[0] if context.pages else context.new_page()
            if not bot.ir_para(crm, bot.CRM_URL):
                raise ErroTecnico("não foi possível abrir o CRM para a fotografia")
            bot.ensure_crm_ready(crm)
            pessoas, total_api, recebidos = fotografar_nao_encontrados(crm)
            caminho_fila = salvar_fotografia(pessoas)
        finally:
            if context is not None:
                context.close()

    print("=" * 72)
    print("FOTOGRAFIA DE NÃO ENCONTRADOS")
    print("=" * 72)
    print(f"Total informado pela API:       {total_api if total_api is not None else 'não informado'}")
    print(f"Itens recebidos:                 {recebidos}")
    print(f"Itens fotografados:              {len(pessoas)}")
    print(f"IDs únicos:                      {len({p.crm_id for p in pessoas})}")
    print(f"CPFs únicos:                     {len({p.cpf for p in pessoas if p.cpf})}")
    print(f"Fotografia congelada:            {caminho_fila}")
    print("=" * 72)
    resposta = input("Digite S para iniciar. Qualquer outra tecla cancela: ").strip().upper()
    if resposta != "S":
        print("Cancelado. Nenhuma alteração foi feita no CRM e o TSE não foi consultado.")
        return 0

    inicio_execucao = agora()
    execucao_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    processos: list[subprocess.Popen[Any]] = []
    for operador in range(TOTAL_OPERADORES):
        comando = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--operador",
            str(operador),
            "--fila",
            str(caminho_fila),
            "--execucao",
            execucao_id,
        ]
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        processos.append(subprocess.Popen(comando, **kwargs))
    codigos = [processo.wait() for processo in processos]
    consolidar(inicio_execucao)
    if any(codigo != 0 for codigo in codigos):
        print(f"Um ou mais operadores terminaram com erro: {codigos}")
        return 1
    return 0


def argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operador", type=int, choices=range(TOTAL_OPERADORES))
    parser.add_argument("--fila", type=Path)
    parser.add_argument("--execucao")
    return parser.parse_args()


def main() -> int:
    args = argumentos()
    if args.operador is None:
        return executar_coordenador()
    if args.fila is None or not args.execucao:
        raise SystemExit("O operador interno precisa receber --fila e --execucao.")
    return executar_operador(
        args.operador,
        args.fila.resolve(),
        args.execucao,
    )


if __name__ == "__main__":
    raise SystemExit(main())
