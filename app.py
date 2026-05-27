0#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Corrige o CPF dos profissionais de um XML CNES (ImportarXMLCNES) usando o
PostgreSQL como fonte da verdade.

Fluxo:
  1. Carrega configuração do .env
  2. Lê o XML de entrada (preservando a estrutura via lxml)
  3. Cria uma cópia do arquivo (o original nunca é alterado)
  4. Conecta ao PostgreSQL
  5. Carrega os profissionais do banco em memória (1 query)
  6. Varre os <DADOS_PROFISSIONAIS> do XML
  7. Compara o CPF do XML com o do banco (busca pelo nome normalizado)
     - igual            -> OK            (nada a fazer)
     - diferente        -> CORRIGIDO     (substitui CPF_PROF na cópia)
     - nome ausente     -> NAO_ENCONTRADO
     - nome ambíguo     -> AMBIGUO       (mais de um CPF distinto p/ o mesmo nome)
  8. Grava a cópia corrigida
  9. Gera relatório CSV
 10. Imprime o resumo final
"""

import csv
import logging
import os
import re
import shutil
import sys
import unicodedata
from collections import defaultdict

from dotenv import load_dotenv
from lxml import etree

import psycopg2
from psycopg2 import sql


# --------------------------------------------------------------------------- #
# Logging — formato "[HH:MM:SS] NÍVEL — mensagem"
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("corrigir_cpf")


# --------------------------------------------------------------------------- #
# Funções auxiliares de normalização
# --------------------------------------------------------------------------- #
def normalizar_nome(s: str) -> str:
    """Remove acentos, coloca em maiúsculas e colapsa espaços."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s


def normalizar_cpf(s: str) -> str:
    """Mantém só os dígitos e completa com zeros à esquerda até 11."""
    if not s:
        return ""
    digitos = re.sub(r"\D", "", s)
    return digitos.zfill(11) if digitos else ""


def perguntar_acao() -> str:
    """Pergunta qual ação executar. Retorna 'cpf' ou 'remover'."""
    while True:
        resp = input(
            "\nO que deseja fazer?\n"
            "  [1] Corrigir CPF dos profissionais (comparar com o banco)\n"
            "  [2] Remover profissionais já cadastrados no banco\n"
            "Escolha 1 ou 2: "
        ).strip()
        if resp == "1":
            log.info("Ação: CORRIGIR CPF")
            return "cpf"
        if resp == "2":
            log.info("Ação: REMOVER profissionais já cadastrados")
            return "remover"
        print("  Opção inválida. Digite 1 ou 2.")


def perguntar_origem() -> str:
    """Pergunta se o XML é o oficial do CNES ou o gerado pela Radar.

    Retorna 'oficial' ou 'radar'.
    """
    while True:
        resp = input(
            "\nEste XML é:\n"
            "  [1] Oficial do CNES (usar como está)\n"
            "  [2] Gerado pela Radar (formatar como o modelo antes)\n"
            "Escolha 1 ou 2: "
        ).strip()
        if resp == "1":
            log.info("Origem do XML: OFICIAL")
            return "oficial"
        if resp == "2":
            log.info("Origem do XML: RADAR (gerado)")
            return "radar"
        print("  Opção inválida. Digite 1 ou 2.")


# --------------------------------------------------------------------------- #
# Etapa 1 — Configuração
# --------------------------------------------------------------------------- #
def carregar_config() -> dict:
    log.info("ETAPA 1/10 — Carregando configuração (.env)")
    load_dotenv()

    obrigatorias = ["PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASSWORD",
                    "DB_TABELA", "DB_COL_NOME", "DB_COL_CPF"]
    cfg = {k: os.getenv(k) for k in [
        "PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASSWORD",
        "DB_TABELA", "DB_COL_NOME", "DB_COL_CPF", "DB_COL_CNS",
        "XML_ENTRADA", "XML_SAIDA", "RELATORIO",
        "XML_SAIDA_REMOVER", "RELATORIO_REMOVER",
    ]}

    faltando = [k for k in obrigatorias if not cfg.get(k)]
    if faltando:
        log.error("Variáveis ausentes no .env: %s", ", ".join(faltando))
        log.error("Copie .env.example para .env e preencha os valores.")
        sys.exit(1)

    cfg.setdefault("XML_ENTRADA", "arquivos/xml_modelo.xml")
    cfg["XML_ENTRADA"] = cfg.get("XML_ENTRADA") or "arquivos/xml_modelo.xml"
    cfg["XML_SAIDA"] = cfg.get("XML_SAIDA") or "arquivos/xml_modelo_corrigido.xml"
    cfg["RELATORIO"] = cfg.get("RELATORIO") or "arquivos/relatorio_cpf.csv"
    cfg["XML_SAIDA_REMOVER"] = cfg.get("XML_SAIDA_REMOVER") or "arquivos/xml_sem_cadastrados.xml"
    cfg["RELATORIO_REMOVER"] = cfg.get("RELATORIO_REMOVER") or "arquivos/relatorio_remocao.csv"

    if not os.path.isfile(cfg["XML_ENTRADA"]):
        log.error("XML de entrada não encontrado: %s", cfg["XML_ENTRADA"])
        sys.exit(1)

    return cfg


# --------------------------------------------------------------------------- #
# Etapas 2 e 3 — Ler XML e criar cópia
# --------------------------------------------------------------------------- #
def ler_e_copiar_xml(cfg: dict, origem: str, saida: str = None):
    saida = saida or cfg["XML_SAIDA"]
    log.info("ETAPA 2/10 — Lendo XML de entrada: %s", cfg["XML_ENTRADA"])
    # Para o XML gerado pela Radar (minificado) removemos os espaços em branco
    # insignificantes para poder reformatar no layout do modelo na gravação.
    # Para o oficial preservamos a formatação original.
    remove_blank = (origem == "radar")
    parser = etree.XMLParser(remove_blank_text=remove_blank)
    tree = etree.parse(cfg["XML_ENTRADA"], parser)

    log.info("ETAPA 3/10 — Criando cópia: %s", saida)
    os.makedirs(os.path.dirname(os.path.abspath(saida)), exist_ok=True)
    shutil.copy2(cfg["XML_ENTRADA"], saida)

    return tree


# --------------------------------------------------------------------------- #
# Etapa 4 — Conexão
# --------------------------------------------------------------------------- #
def conectar(cfg: dict):
    log.info("ETAPA 4/10 — Conectando ao PostgreSQL (%s:%s/%s)",
             cfg["PG_HOST"], cfg["PG_PORT"], cfg["PG_DB"])
    try:
        conn = psycopg2.connect(
            host=cfg["PG_HOST"],
            port=cfg["PG_PORT"],
            dbname=cfg["PG_DB"],
            user=cfg["PG_USER"],
            password=cfg["PG_PASSWORD"],
        )
    except psycopg2.Error as e:
        log.error("Falha ao conectar: %s", e)
        sys.exit(1)
    return conn


def _identificador_tabela(nome: str) -> sql.Composed:
    """Aceita 'schema.tabela' ou 'tabela' e devolve um Identifier seguro."""
    partes = nome.split(".")
    return sql.SQL(".").join(sql.Identifier(p) for p in partes)


# --------------------------------------------------------------------------- #
# Etapa 5 — Carregar profissionais do banco em memória
# --------------------------------------------------------------------------- #
def carregar_banco(conn, cfg: dict) -> dict:
    log.info("ETAPA 5/10 — Carregando profissionais do banco (%s)", cfg["DB_TABELA"])

    col_cns = cfg.get("DB_COL_CNS")
    cols = [sql.Identifier(cfg["DB_COL_NOME"]), sql.Identifier(cfg["DB_COL_CPF"])]
    if col_cns:
        cols.append(sql.Identifier(col_cns))

    query = sql.SQL("SELECT {cols} FROM {tab}").format(
        cols=sql.SQL(", ").join(cols),
        tab=_identificador_tabela(cfg["DB_TABELA"]),
    )

    # nome_normalizado -> { cpf_normalizado: cns }
    indice = defaultdict(dict)
    total = 0
    with conn.cursor() as cur:
        cur.execute(query)
        for row in cur:
            nome = row[0]
            cpf = normalizar_cpf(row[1])
            cns = row[2] if (col_cns and len(row) > 2) else ""
            if not nome or not cpf:
                continue
            indice[normalizar_nome(nome)][cpf] = cns
            total += 1

    log.info("        %d registros lidos, %d nomes distintos", total, len(indice))
    return indice


# --------------------------------------------------------------------------- #
# Etapas 6 e 7 — Varrer XML, comparar e decidir
# --------------------------------------------------------------------------- #
def processar(tree, indice: dict):
    log.info("ETAPA 6/10 — Varrendo profissionais do XML")
    profissionais = tree.findall(".//DADOS_PROFISSIONAIS")
    log.info("        %d profissionais encontrados no XML", len(profissionais))

    log.info("ETAPA 7/10 — Comparando CPFs (busca por nome)")
    linhas_relatorio = []
    contagem = defaultdict(int)

    for elem in profissionais:
        nome_xml = elem.get("NM_PROF", "")
        cns_xml = elem.get("CO_CNS", "")
        cpf_xml = normalizar_cpf(elem.get("CPF_PROF", ""))

        chave = normalizar_nome(nome_xml)
        cpfs_banco = indice.get(chave)

        if not cpfs_banco:
            status, cpf_banco, obs = "NAO_ENCONTRADO", "", "nome não localizado no banco"
        elif len(cpfs_banco) > 1:
            status, cpf_banco = "AMBIGUO", ""
            obs = "mais de um CPF no banco: " + ", ".join(sorted(cpfs_banco))
        else:
            cpf_banco = next(iter(cpfs_banco))
            if cpf_banco == cpf_xml:
                status, obs = "OK", ""
            else:
                elem.set("CPF_PROF", cpf_banco)
                status, obs = "CORRIGIDO", f"{cpf_xml} -> {cpf_banco}"

        contagem[status] += 1
        linhas_relatorio.append({
            "status": status,
            "nm_prof": nome_xml,
            "co_cns": cns_xml,
            "cpf_xml": cpf_xml,
            "cpf_banco": cpf_banco,
            "obs": obs,
        })

        if status == "CORRIGIDO":
            log.info("        CORRIGIDO: %s (%s)", nome_xml, obs)

    return linhas_relatorio, contagem


# --------------------------------------------------------------------------- #
# Etapa 8 — Gravar a cópia corrigida
# --------------------------------------------------------------------------- #
def gravar_xml(tree, cfg: dict, origem: str, saida: str = None):
    saida = saida or cfg["XML_SAIDA"]
    log.info("ETAPA 8/10 — Gravando XML: %s", saida)

    if origem == "radar":
        # Reformatar no layout do xml_modelo.xml: uma tag por linha, sem
        # indentação (flush-left). Como os elementos do CNES não têm texto
        # entre tags, inserir uma quebra de linha entre cada ">" e "<".
        corpo = etree.tostring(tree, encoding="unicode")
        corpo = re.sub(r">\s*<", ">\n<", corpo).strip()
        with open(saida, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write(corpo)
            f.write("\n")
        log.info("        XML reformatado no layout do modelo (flush-left)")
    else:
        # Oficial: preserva a formatação original.
        tree.write(
            saida,
            encoding="UTF-8",
            xml_declaration=True,
        )


# --------------------------------------------------------------------------- #
# Etapa 9 — Relatório CSV
# --------------------------------------------------------------------------- #
def gerar_relatorio(linhas, caminho: str, campos: list):
    log.info("ETAPA 9/10 — Gerando relatório CSV: %s", caminho)
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        writer.writeheader()
        writer.writerows(linhas)


# --------------------------------------------------------------------------- #
# Etapa 10 — Resumo
# --------------------------------------------------------------------------- #
def resumo(contagem: dict):
    log.info("ETAPA 10/10 — Resumo final")
    total = sum(contagem.values())
    for status in ["OK", "CORRIGIDO", "NAO_ENCONTRADO", "AMBIGUO"]:
        log.info("        %-15s: %d", status, contagem.get(status, 0))
    log.info("        %-15s: %d", "TOTAL", total)


# --------------------------------------------------------------------------- #
# Ação alternativa — Remover profissionais já cadastrados no banco
# --------------------------------------------------------------------------- #
def remover_cadastrados(tree, indice: dict):
    """Remove do XML cada <DADOS_PROFISSIONAIS> cujo nome já existe no banco."""
    log.info("ETAPA 6/10 — Varrendo profissionais do XML")
    profissionais = tree.findall(".//DADOS_PROFISSIONAIS")
    log.info("        %d profissionais encontrados no XML", len(profissionais))

    log.info("ETAPA 7/10 — Removendo profissionais já cadastrados (busca por nome)")
    linhas_relatorio = []
    contagem = defaultdict(int)

    for elem in profissionais:
        nome_xml = elem.get("NM_PROF", "")
        cns_xml = elem.get("CO_CNS", "")
        cpf_xml = normalizar_cpf(elem.get("CPF_PROF", ""))

        chave = normalizar_nome(nome_xml)
        if chave in indice:
            status, obs = "REMOVIDO", "nome localizado no banco"
            elem.getparent().remove(elem)
            log.info("        REMOVIDO: %s", nome_xml)
        else:
            status, obs = "MANTIDO", "nome não localizado no banco"

        contagem[status] += 1
        linhas_relatorio.append({
            "status": status,
            "nm_prof": nome_xml,
            "co_cns": cns_xml,
            "cpf_xml": cpf_xml,
            "obs": obs,
        })

    return linhas_relatorio, contagem


def resumo_remocao(contagem: dict):
    log.info("ETAPA 10/10 — Resumo final")
    total = sum(contagem.values())
    for status in ["REMOVIDO", "MANTIDO"]:
        log.info("        %-15s: %d", status, contagem.get(status, 0))
    log.info("        %-15s: %d", "TOTAL", total)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = carregar_config()
    acao = perguntar_acao()
    origem = perguntar_origem()

    if acao == "cpf":
        tree = ler_e_copiar_xml(cfg, origem)
        conn = conectar(cfg)
        try:
            indice = carregar_banco(conn, cfg)
        finally:
            conn.close()

        linhas, contagem = processar(tree, indice)
        gravar_xml(tree, cfg, origem)
        gerar_relatorio(linhas, cfg["RELATORIO"],
                        ["status", "nm_prof", "co_cns", "cpf_xml", "cpf_banco", "obs"])
        resumo(contagem)
    else:
        tree = ler_e_copiar_xml(cfg, origem, cfg["XML_SAIDA_REMOVER"])
        conn = conectar(cfg)
        try:
            indice = carregar_banco(conn, cfg)
        finally:
            conn.close()

        linhas, contagem = remover_cadastrados(tree, indice)
        gravar_xml(tree, cfg, origem, cfg["XML_SAIDA_REMOVER"])
        gerar_relatorio(linhas, cfg["RELATORIO_REMOVER"],
                        ["status", "nm_prof", "co_cns", "cpf_xml", "obs"])
        resumo_remocao(contagem)

    log.info("Concluído com sucesso.")


if __name__ == "__main__":
    main()
