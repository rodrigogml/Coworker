---
name: cpfl
description: Validar links individuais oficiais da CPFL e obter PIX ou linha digitável em arquivo privado usando o CPF protegido da pessoa titular. Usar quando a tarefa mencionar link de conta de energia, fatura, PIX, código de barras ou PDF da CPFL.
---

# Consultar contas da CPFL

Usar `python skills/cpfl/scripts/cpfl.py`. Esta skill não possui configuração privada,
perfil de integração nem dependência de Gmail. Nunca fornecer CPF, link individual ou
código de pagamento diretamente em argumentos.

## Executar o fluxo

1. Obter o link individual por uma fonte autorizada, sem expô-lo ao modelo ou ao log.
2. Resolver a entrada da pessoa titular no cofre.
3. Usar `doctor` quando for necessário validar a presença e proteção do CPF.
4. Usar `payment-data` com o link em arquivo privado ou na entrada padrão.
5. Para PDF, usar o navegador conforme a seção específica.

```powershell
python skills/cpfl/scripts/cpfl.py doctor `
  --entity-ref "Pessoas/Fisicas/Nome Completo"
python skills/cpfl/scripts/cpfl.py payment-data `
  --entity-ref "Pessoas/Fisicas/Nome Completo" `
  --link-file data/work/cpfl/link.txt
```

`payment-data` grava valores somente em `data/work/cpfl/` e devolve metadados de
validação. Não abrir nem reproduzir o arquivo quando a tarefa pedir apenas conferência.

## Baixar o PDF

O botão **Veja sua conta** usa reCAPTCHA. Não tentar contorná-lo por HTTP.

1. Receber o mesmo link oficial por canal protegido.
2. Abrir o link no navegador controlado.
3. Ler `CPF` internamente por `scripts/vault_entities.py` e preencher somente os quatro
   primeiros dígitos.
4. Antes de resolver o reCAPTCHA, seguir a política de confirmação do navegador.
5. Se houver desafio visual, deixar a aba aberta para a pessoa usuária concluí-lo.
6. Capturar o evento de download e salvar o PDF dentro de `data/`.

Depois do download, usar a skill de PDF para extrair e validar os campos solicitados.

## Respeitar contratos

- Aceitar somente HTTPS no host `contadigital.cpfl.com.br` e no caminho oficial fixo.
- Receber o link somente por `--link-file` ou `--link-stdin`; nunca por argumento.
- Obter CPF exclusivamente da propriedade protegida da pessoa titular.
- Nunca imprimir link individual, CPF, PIX, linha digitável, `VIEWSTATE` ou cookies.
- Não armazenar códigos de pagamento fora de `data/`.
- Obter dados de pagamento não autoriza realizar pagamento.
- Não repetir automaticamente um POST após timeout ou resposta ambígua.
