"""Mensagens curtas e configuráveis de andamento da interface Telegram."""

from __future__ import annotations

import secrets


IMMEDIATE_MESSAGES = (
    "Já estou vendo isso.",
    "Deixa comigo — já estou analisando.",
    "Estou olhando agora.",
    "Já comecei por aqui.",
    "Peguei. Vou verificar.",
    "Vi sua mensagem e já comecei.",
    "Estou nisso agora.",
    "Já abri a solicitação.",
    "Começando a analisar.",
    "Entendido. Já estou cuidando.",
    "Já estou verificando.",
    "Recebi e já entrei no assunto.",
    "Vou direto ao ponto — já comecei.",
    "Estou conferindo agora.",
    "Já coloquei as engrenagens para girar.",
    "Analisando neste momento.",
    "Estou cuidando disso.",
    "Já estou trabalhando na resposta.",
    "Peguei a ideia. Já estou nela.",
    "Comecei a processar sua solicitação.",
    "Estou avaliando isso agora.",
    "Mensagem vista. Mãos à obra.",
    "Já estou levantando as informações.",
    "Vou verificar isso agora.",
    "Estou preparando a resposta.",
    "Já iniciei a análise.",
    "Estou resolvendo por aqui.",
    "Vi e já estou trabalhando nisso.",
    "Comecei. Assim que concluir, respondo aqui.",
    "Já estou tratando sua solicitação.",
)

QUEUED_MESSAGES = (
    "Vejo na sequência.",
    "Já te respondo — entrou na fila.",
    "Anotado. Pego isso assim que terminar o atual.",
    "Está na sequência.",
    "Recebi. Vou tratar logo depois do que está em andamento.",
    "Entrou na fila; já chego nela.",
    "Deixei na sequência de processamento.",
    "Peguei. Assim que liberar, começo.",
    "Está comigo e entrou na fila.",
    "Recebido. Vou chegar nisso em breve.",
    "Já deixei organizado na fila.",
    "Assim que concluir o atual, avanço na fila.",
    "Está aguardando processamento.",
    "Recebi e coloquei na sequência.",
    "Fica na fila por um instante; já chego lá.",
    "Anotado. Respondo assim que chegar a vez.",
    "Entrou na sequência certinho.",
    "Já está aguardando aqui.",
    "Recebi. Ela será processada na ordem.",
    "Mensagem guardada na fila — sem cair no limbo digital.",
    "Está na fila e eu sigo pela sequência.",
    "Peguei sua mensagem. Já chego nela.",
    "Vou ver isso na sequência.",
    "Recebido. Está aguardando a vez.",
    "Coloquei na fila de trabalho.",
    "Já está na sequência para análise.",
    "Assim que eu liberar a tarefa atual, continuo pela fila.",
    "Está registrado e aguardando processamento.",
    "Recebi. Retomo com você assim que processar.",
    "Na fila, mas não esquecida.",
)

# O Bot API aceita somente um conjunto específico de reações para bots.
# Mantenha esta coleção restrita às reações normais documentadas pelo Telegram.
PROCESSING_REACTIONS = ("🫡", "👀", "🤔", "🤓")
QUEUE_REACTION = "🆒"
COMPLETED_REACTION = "💯"


def choose_message(messages: tuple[str, ...]) -> str:
    """Escolhe uma variação sem manter estado ou previsibilidade desnecessária."""
    return secrets.choice(messages)


def choose_processing_reaction() -> str:
    """Escolhe uma reação de processamento sem manter estado entre mensagens."""
    return secrets.choice(PROCESSING_REACTIONS)
