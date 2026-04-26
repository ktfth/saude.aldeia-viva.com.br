# Deploy

O projeto foi preparado para deploy em Vercel com FastAPI.

## Estratégia atual

- a aplicação principal fica em `app.py`
- o snapshot consolidado é embarcado em `bundled_report_snapshot.py`
- a página pública e os endpoints consomem o mesmo estado agregado

Isso reduz a dependência de download no runtime e evita que o deploy suba vazio quando uma fonte externa estiver indisponível.

## Passos

1. Conecte o repositório ao projeto da Vercel.
2. Faça o deploy pela CLI ou pelo painel.
3. Verifique `/health` depois da publicação.
4. Abra `/dashboard` e confirme se há municípios e alertas.

## Variáveis úteis

- `SINAN_YEAR`
- `SINAN_DISEASE_CODES`
- `SINAN_REQUEST_TIMEOUT_SECONDS`
- `SINAN_CACHE_DIR`
- `SINAN_FORCE_REFRESH`
- `SINAN_DISABLE_CACHE`
- `SINAN_DISABLE_REPORT_CACHE`

## Verificações pós-deploy

- `GET /health`
- `GET /v1/diseases`
- `GET /v1/risk-index?estado=SP&limite=5`
- `GET /dashboard`

## Observação sobre bundles

Se o ambiente estiver sem acesso de rede ou com limitações temporárias, o snapshot embarcado mantém a experiência funcional.

