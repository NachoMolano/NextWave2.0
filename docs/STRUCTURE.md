# Siete carpetas — propuesta de estructura

**Esto es una propuesta, no lo que está en el repo.** `docs/ARCHITECTURE.md` documenta la
estructura actual (once paquetes) y sigue siendo la referencia de lo que corre hoy. Este
documento describe cómo se vería con siete, por qué cada fusión tiene una razón, y qué no
cambia.

El objetivo no es tener menos carpetas por gusto. Es que el árbol se pueda explicar en una
oración sin perder ninguna de las garantías.

---

## 1. La medición que la motiva

Líneas de código por paquete, sin comentarios ni vacías, sobre `origin/martin`:

| Paquete | Líneas | | Paquete | Líneas |
| --- | ---: | --- | --- | ---: |
| `voice/` | 742 | | `notify/` | 119 |
| `telephony/` | 292 | | `ledger/` | 53 |
| `repo/` | 287 | | `policy/` | **6** |
| `agent/` | 190 | | `tools/` | **6** |
| `domain/` | 189 | | `market/` | **5** |

Todo el backend son **2.257 líneas**. Dos hechos que salen de ahí:

- Los seis `__init__.py` que son solo docstring suman **50 líneas** — el 2% del código. El
  árbol de carpetas casi no cuesta, así que reducirlo no es una optimización de tamaño.
- Tres paquetes tienen **17 líneas entre los tres**, y son justo los que sostienen la tesis
  del proyecto. No están mal diseñados: están sin escribir.

La razón para bajar a siete no es el costo. Es que once cajas para 2.257 líneas son más
difíciles de explicar de lo que el sistema es en realidad.

## 2. Las cuatro fusiones

Cada una tiene un motivo, no es reordenar por gusto.

### `telephony/` → `voice/`  (292 + 742)

Las dos cargan el audio de un desconocido: **mismo nivel de confianza, mismo modo de falla**.
La separación se defendía por "dos proveedores distintos", pero lo que de verdad da valor no
es la carpeta: es el `Protocol` `AudioSource` en `frames.py`, que permite correr el pipeline
sin línea telefónica. Ese Protocol sobrevive intacto dentro de `voice/`.

La carpeta nunca fue la costura. El Protocol sí.

### `ledger/` → `store/`  (53 + 287)

Mismo nivel de confianza — obedecen, no deciden — y hoy cambian juntos. La regla de
`ARCHITECTURE.md` §2 dice que dos candidatos que cambian juntos al mismo nivel de confianza
son un solo directorio; `ledger/` con 53 líneas y una clase todavía no la pasa.

Se vuelve a partir el día que "qué cuenta como evidencia" cambie por razones distintas a
"cómo se guarda".

### `market/` → `tools/`  (5 líneas)

Es una promesa con forma de carpeta. Nace como módulo dentro de `tools/` y se separa cuando
tenga estrategia multi-carrier de verdad.

### `voice/stt/` y `voice/tts/` → `voice/stt.py` y `voice/tts.py`

Seis archivos para un proveedor y un fake cada uno. El `Protocol` va arriba del módulo y los
fakes se mueven a `tests/`, que es donde vive un doble de prueba. De seis archivos a dos, y
118 líneas menos en el paquete de producción.

### Además, un renombre

`agent/models.py` contiene `OpenAIRecapModel`, un adaptador de proveedor, mientras
`domain/models.py` contiene los tipos del dominio. **Dos archivos llamados `models.py` que
significan cosas distintas.** Pasa a `agent/recap_model.py`. Cuesta cero y quita una
confusión que aparece cada vez que alguien abre el árbol.

## 3. Cómo queda

```
backend/app/
  domain/    tipos compartidos. no importa nada.
  policy/    decide. importa solo domain.
  tools/     la frontera: único lugar donde una propuesta encuentra a policy.
  agent/     prompts y extracción. contenido, no lógica.
  voice/     el teléfono y el audio: Twilio, Deepgram, el modelo.
  store/     persistencia y evidencia.
  notify/    lo que sale por escrito.
  config.py  variables de entorno. el único que lee os.environ.
  main.py    el cableado.
```

Y el contrato completo cabe en una pantalla:

```python
ALLOWED = {
    "domain": set(),
    "config": set(),
    "policy": {"domain"},
    "store":  {"domain", "config"},
    "notify": {"domain", "config"},
    "agent":  {"domain"},
    "tools":  {"domain", "policy", "store", "notify"},
    "voice":  {"domain", "config", "agent", "tools"},
}
```

## 4. La oración

El problema de comunicación no se arregla renombrando carpetas, sino teniendo una frase que
alguien pueda repetir:

> Siete carpetas. Una tiene los tipos, una decide, una es la frontera, y las otras cuatro
> hablan con proveedores. La que decide no puede importar a ninguna de las que escuchan, y
> eso lo verifica un test.

El mapa `ALLOWED` de arriba es la prueba de esa frase, y son ocho líneas.

## 5. Lo que no cambia

Ninguna garantía se debilita. `policy/` sigue siendo un sumidero:

- no puede llamar a un modelo — no puede importar `voice/`
- no puede tocar la red — no puede importar `store/` ni `notify/`
- no puede leer un prompt — no puede importar `agent/`

Los tres tests siguen valiendo igual: imports contra `ALLOWED`, ningún paquete sin declarar,
y el grafo acíclico. El invariante #1 sigue siendo una propiedad del grafo de imports, no una
regla que alguien tenga que recordar.

## 6. Lo que se consideró y se descartó

**Cinco carpetas nombradas por confianza** — `domain`, `untrusted`, `policy`, `trusted`,
`tools`. Se comunica precioso: el árbol *es* el argumento, y se explica en cinco segundos en
vez de diez.

No funciona. `untrusted/` tendría 1.034 líneas y terminaría con `untrusted/telephony/` y
`untrusted/voice/` adentro — las mismas carpetas, un nivel más hondo, y peor para navegar. La
claridad era prestada: se veía bien en el diagrama y se sentía peor al trabajar.

**Borrar `policy/`, `tools/` y `market/` por estar vacíos.** Son 17 líneas y podrían quitarse
hoy sin romper nada. Pero `policy/` vacío no es una carpeta que sobra: es la tesis del
proyecto sin implementar. Borrarla porque no tiene código es borrar el argumento en vez de
escribirlo.

## 7. Costo y momento

Mover archivos y ajustar imports: dos o tres horas, cero lógica nueva. El test de capas dice
al instante si algo se rompió, así que el riesgo es bajo.

Pero no mueve la aguja del demo. Con `policy/`, `tools/` y `market/` en 17 líneas, lo que le
falta a la tesis no son carpetas — son las primeras 200 líneas de `policy/`. Esta propuesta
se ejecuta después del pitch, o no se ejecuta.
