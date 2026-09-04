# HelixGrid – guía fácil en español

## ¿Qué es HelixGrid?

HelixGrid ejecuta workflows con dependencias en workers. El **coordinator** gestiona estados, dependencias, retries y leases. Los **workers** ejecutan los comandos.

## 1. Requisitos

- Git
- Docker con Docker Compose
- Python 3.11+ para la CLI

## 2. Descargar

```bash
git clone https://github.com/chingchang2000/app.git
cd app
```

## 3. Iniciar HelixGrid

```bash
docker compose up --build --scale worker=3
```

Después abre:

```text
http://127.0.0.1:8080/healthz
```

La respuesta debe contener `"status":"ok"`.

## 4. Instalar la CLI

Windows:

```powershell
py -m pip install -e .\sdk\python
```

Linux/macOS:

```bash
python3 -m pip install -e ./sdk/python
```

## 5. Ejecutar el ejemplo

Windows:

```powershell
py -m helixgrid.cli submit examples\workflow.json --wait
```

Linux/macOS:

```bash
python3 -m helixgrid.cli submit examples/workflow.json --wait
```

Si todo funciona, el estado final será `SUCCEEDED`.

## 6. Comandos útiles

```bash
python3 -m helixgrid.cli list
python3 -m helixgrid.cli get WORKFLOW_ID
python3 -m helixgrid.cli workers
python3 -m helixgrid.cli watch WORKFLOW_ID
docker compose logs -f
docker compose down
```

En Windows puedes usar `py` en lugar de `python3`.

## 7. Crear tu propio workflow

```json
{
  "name": "mi-workflow",
  "tasks": [
    {
      "id": "hello",
      "command": ["sh", "-lc", "echo Hola desde HelixGrid"]
    },
    {
      "id": "done",
      "depends_on": ["hello"],
      "command": ["sh", "-lc", "echo Terminado"]
    }
  ]
}
```

Ejecuta:

```bash
python3 -m helixgrid.cli submit my-workflow.json --wait
```

## 8. Tests

```bash
make test
make check
```

## 9. Almacenamiento

El coordinator activo usa actualmente un **almacenamiento en memoria**. Si se reinicia el coordinator, se pierde el estado de workflows y workers.

`storage/postgres/schema.sql` contiene el diseño PostgreSQL y CI valida el schema, pero todavía no está conectado como base de datos activa del coordinator.

## 10. Solución de problemas

Logs de workers:

```bash
docker compose logs worker
```

Ver un workflow:

```bash
python3 -m helixgrid.cli get WORKFLOW_ID
```

Comprobar Docker:

```bash
docker compose config
```

## 11. Seguridad

Los workers ejecutan los comandos recibidos en los workflows y el coordinator todavía no tiene autenticación.

No expongas directamente el puerto 8080 a Internet sin añadir autenticación y protección de red.
