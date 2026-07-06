from fastapi import FastAPI, Request, Form, Query, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from databases import Database
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import math
import json
from datetime import datetime
from uuid import uuid4
from db import init_engine, fetch_one, fetch_all, execute
from openai import OpenAI

DATABASE_URL = "postgresql://cloudflowz:Fr33dumb123!@localhost:5444/cloudflowz_dev"
# Was "svx" -- pointed at the wrong schema for editing Playvocal content.
# get_crud_tables()/get_table_columns()/etc. all filter by this value, so
# nothing under playvocal.* would show up in the tool until this matched.
SCHEMA_NAME = "playvocal"

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory="templates")
database = Database(DATABASE_URL)

@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

@app.get("/health")
async def health_check():
    return {"status": "ok"}

# ----------------------------
# Helper functions
# ----------------------------
async def get_crud_tables():
    query = """
        SELECT t.table_name
        FROM information_schema.tables t
        LEFT JOIN pg_catalog.pg_description d
            ON d.objoid = (t.table_schema||'.'||t.table_name)::regclass
        WHERE t.table_schema = :schema
        AND COALESCE(d.description,'')='CRUD'
        AND t.table_type='BASE TABLE'
        ORDER BY t.table_name
    """
    rows = await database.fetch_all(query=query, values={"schema": SCHEMA_NAME})
    return [r[0] for r in rows]

async def get_table_columns(table_name: str):
    """
    Returns a list of tuples:
        (column_name, data_type, is_locked, character_maximum_length)
    """
    query = """
        SELECT
            column_name,
            data_type,
            character_maximum_length,
            CASE WHEN ordinal_position = 1 THEN TRUE ELSE FALSE END AS is_locked
        FROM information_schema.columns
        WHERE table_schema = :schema
          AND table_name = :table
        ORDER BY ordinal_position
    """
    rows = await database.fetch_all(query=query, values={"schema": SCHEMA_NAME, "table": table_name})
    return [(r['column_name'], r['data_type'], r['is_locked'], r['character_maximum_length']) for r in rows]

async def get_table_rows(
    table_name: str,
    page: int = 1,
    per_page: int = 20,
    sort: str | None = None,
    sort_dir: str = "asc"
):
    offset = (page - 1) * per_page

    # Treat "None" string as actual None
    if sort in ("", "None"):
        sort = None

    if sort:
        sort_dir = (sort_dir or "asc").lower()
        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"
        query = f'SELECT * FROM "{SCHEMA_NAME}"."{table_name}" ORDER BY "{sort}" {sort_dir} LIMIT :limit OFFSET :offset'
    else:
        query = f'SELECT * FROM "{SCHEMA_NAME}"."{table_name}" ORDER BY 1 LIMIT :limit OFFSET :offset'

    rows = await database.fetch_all(query=query, values={"limit": per_page, "offset": offset})
    return [dict(r) for r in rows]

async def get_total_rows(table_name: str):
    query = f'SELECT COUNT(*) FROM "{SCHEMA_NAME}"."{table_name}"'
    row = await database.fetch_one(query=query)
    return row[0]

# ----------------------------
# Required Column helper
# ----------------------------
async def get_required_columns(table_name: str):
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = :schema
          AND table_name = :table
          AND is_nullable = 'NO'
          AND ordinal_position > 1
    """
    rows = await database.fetch_all(
        query=query,
        values={"schema": SCHEMA_NAME, "table": table_name}
    )
    return {r[0] for r in rows}

async def get_locked_columns(table: str):
    query = """
        SELECT
            c.column_name,
            c.ordinal_position,
            pgd.description
        FROM information_schema.columns c
        LEFT JOIN pg_catalog.pg_description pgd
            ON pgd.objoid = (c.table_schema||'.'||c.table_name)::regclass
           AND pgd.objsubid = c.ordinal_position
        WHERE c.table_schema = :schema
          AND c.table_name = :table
        ORDER BY c.ordinal_position
    """
    rows = await database.fetch_all(
        query=query,
        values={"schema": SCHEMA_NAME, "table": table}
    )

    columns = [r["column_name"] for r in rows]

    locked_columns = {
        r["column_name"]: (
            r["ordinal_position"] == 1 or
            (r["description"] or "").strip().upper() == "LOCKED"
        )
        for r in rows
    }

    return columns, locked_columns

# ----------------------------
# Render table helper
# ----------------------------
async def render_table(
    request: Request,
    selected_table: str,
    page: int = 1,
    per_page: int = 20,
    sort: str | None = None,
    sort_dir: str = "asc",
    selected_row_id: int | None = None,
    edit_mode: bool = False
):
    columns_raw = await get_table_columns(selected_table)
    columns = [col[0] for col in columns_raw]
    data_types = {col[0]: col[1] for col in columns_raw}

    rows = await get_table_rows(selected_table, page=page, per_page=per_page, sort=sort, sort_dir=sort_dir)
    total_rows = await get_total_rows(selected_table)
    total_pages = max(1, math.ceil(total_rows / per_page))

    return templates.TemplateResponse("crud_content.html", {
        "request": request,
        "columns": columns,
        "data_types": data_types,
        "rows": rows,
        "selected_table": selected_table,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "sorted_column": sort,
        "sort_dir": sort_dir,
        "selected_row_id": selected_row_id,
        "edit_mode": edit_mode
    })

# ----------------------------
# Type casting for edits
# ----------------------------
def cast_value(value: str, data_type: str):
    """Convert a string from form input to the correct Python type for Postgres."""
    if value is None:
        return None

    data_type = data_type.lower()

    # Boolean
    if data_type in ("boolean", "bool"):
        return str(value).lower() in ("true", "1", "t", "on")

    # Integers
    if data_type in ("integer", "int", "smallint", "bigint"):
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    # Floats / numerics
    if data_type in ("numeric", "real", "double precision", "decimal"):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    # Date / timestamp
    if data_type in (
        "timestamp without time zone",
        "timestamp with time zone",
        "date"
    ):
        try:
            # Try parsing full datetime with microseconds
            dt = datetime.fromisoformat(value)
            # If column is just 'date', return date part only
            if data_type == "date":
                return dt.date()
            return dt
        except ValueError:
            # fallback: try date-only format
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                return None

    # Default: leave as string
    return value

# ----------------------------
# Routes
# ----------------------------
@app.get("/", response_class=HTMLResponse)
async def crud_index(request: Request):
    tables = await get_crud_tables()
    if not tables:
        return HTMLResponse("No CRUD tables found in schema.")
    
    selected_table = tables[0]

    # Get total rows and pages for this table
    total_rows = await get_total_rows(selected_table)
    per_page = 20
    total_pages = max(1, math.ceil(total_rows / per_page))

    return templates.TemplateResponse("crud_swap.html", {
        "request": request,
        "tables": tables,
        "selected_table": selected_table,
        "schema": SCHEMA_NAME,
        "page": 1,
        "per_page": per_page,
        "total_pages": total_pages,
        "sorted_column": None,
        "sort_dir": "asc"
    })

@app.get("/crud_table", response_class=HTMLResponse)
async def crud_table(
    request: Request,
    selected_table: str = Query(...),
    page: int = Query(1),
    per_page: int = Query(20),
    sort: str | None = Query(None),
    sort_dir: str | None = Query(None),
    selected_row_id: int | None = Query(None)  # NEW
):
    # Toggle sort direction
    if not sort_dir:
        sort_dir = "asc"
    elif sort_dir.lower() == "asc":
        sort_dir = "desc"
    else:
        sort_dir = "asc"

    return await render_table(
        request,
        selected_table,
        page,
        per_page,
        sort,
        sort_dir=sort_dir,
        selected_row_id=selected_row_id  # pass to template
    )

@app.get("/crud", response_class=HTMLResponse)
async def crud_alias(request: Request):
    return await crud_index(request)

# ----------------------------
# Add row
# ----------------------------
@app.post("/crud/add", response_class=HTMLResponse)
async def add_row(request: Request, table: str = Form(...), **kwargs):
    columns_raw = await get_table_columns(table)
    columns = [col[0] for col in columns_raw]
    data_types = {col[0]: col[1] for col in columns_raw}
    insert_cols = columns[1:]  # skip PK
    insert_vals = {k: cast_value(v, data_types[k]) for k, v in kwargs.items() if k in insert_cols}
    if insert_cols:
        col_names = ','.join(f'"{c}"' for c in insert_cols)
        val_names = ','.join(f':{c}' for c in insert_cols)
        query = f'INSERT INTO "{SCHEMA_NAME}"."{table}" ({col_names}) VALUES ({val_names})'
        await database.execute(query=query, values=insert_vals)
    return await render_table(request, selected_table=table)

# ----------------------------
# Delete row
# ----------------------------
@app.post("/crud/delete", response_class=HTMLResponse)
async def delete_row(request: Request, table: str = Form(...), id: int = Form(...)):
    columns_raw = await get_table_columns(table)
    if not columns_raw:
        return HTMLResponse("Table not found or has no columns", status_code=400)
    pk_column = columns_raw[0][0]
    try:
        await database.execute(
            f'DELETE FROM "{SCHEMA_NAME}"."{table}" WHERE "{pk_column}" = :id',
            values={"id": id}
        )
    except Exception as e:
        return HTMLResponse(f"Error deleting row: {str(e)}", status_code=500)
    return await render_table(request, selected_table=table, page=1, per_page=20)

# ----------------------------
# Inline edit form
# ----------------------------
@app.get("/crud_edit_form", response_class=HTMLResponse)
async def edit_form(
    request: Request,
    table: str = Query(...),
    id: int = Query(...)
):
    try:
        query = """
            SELECT
                c.column_name,
                c.data_type,
                c.character_maximum_length,
                pgd.description AS comment
            FROM information_schema.columns c
            LEFT JOIN pg_catalog.pg_description pgd
                ON pgd.objoid = (c.table_schema||'.'||c.table_name)::regclass
               AND pgd.objsubid = c.ordinal_position
            WHERE c.table_schema = :schema
              AND c.table_name = :table
            ORDER BY c.ordinal_position
        """
        rows = await database.fetch_all(
            query=query,
            values={"schema": SCHEMA_NAME, "table": table}
        )

        if not rows:
            return HTMLResponse("Table has no columns", status_code=400)

        columns = [r["column_name"] for r in rows]
        data_types = {r["column_name"]: r["data_type"] for r in rows}
        column_lengths = {r["column_name"]: r["character_maximum_length"] for r in rows}  # NEW

        # ✅ LOCKED columns (PK or comment=LOCKED)
        locked_columns = {
            r["column_name"]: (
                r["column_name"] == columns[0] or (r["comment"] or "").upper() == "LOCKED"
            )
            for r in rows
        }

        required_columns = await get_required_columns(table)

        pk_column = columns[0]
        row = await database.fetch_one(
            f'SELECT * FROM "{SCHEMA_NAME}"."{table}" WHERE "{pk_column}" = :id',
            {"id": id}
        )

        if not row:
            return HTMLResponse("Row not found", status_code=404)

        return templates.TemplateResponse(
            "crud_edit_form.html",
            {
                "request": request,
                "row": dict(row),
                "columns": columns,
                "selected_table": table,
                "data_types": data_types,
                "column_lengths": column_lengths,  # PASS LENGTHS
                "locked_columns": locked_columns,
                "required_columns": required_columns,
                "insert_mode": False
            }
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return HTMLResponse(str(e), status_code=500)

# ----------------------------
# Save edited row
# ----------------------------
@app.post("/crud/edit", response_class=HTMLResponse)
async def edit_row(request: Request):
    form = await request.form()
    table = form.get("table")
    id_val = form.get("id")

    if not table or not id_val:
        return HTMLResponse("Missing table or ID", status_code=400)

    try:
        id_int = int(id_val)
    except ValueError:
        return HTMLResponse("Invalid ID", status_code=400)

    # Column metadata
    columns_raw = await get_table_columns(table)
    columns = [c[0] for c in columns_raw]
    data_types = {c[0]: c[1] for c in columns_raw}

    # Get locked columns from comments and PK
    query = """
        SELECT column_name, pgd.description
        FROM information_schema.columns c
        LEFT JOIN pg_catalog.pg_description pgd
            ON pgd.objoid = (c.table_schema||'.'||c.table_name)::regclass
           AND pgd.objsubid = c.ordinal_position
        WHERE c.table_schema = :schema
          AND c.table_name = :table
    """
    rows = await database.fetch_all(query=query, values={"schema": SCHEMA_NAME, "table": table})
    locked_columns = {r["column_name"]: (r["column_name"] == columns[0] or r["description"] == "LOCKED") for r in rows}

    # NOT NULL columns
    required_columns = await get_required_columns(table)

    # Only update editable columns that exist in the form and are NOT locked
    update_cols = [c for c in columns if c in form and not locked_columns.get(c, False)]
    values = {}

    for c in update_cols:
        raw = form.get(c)
        if c in required_columns and str(raw).strip() == "":
            return HTMLResponse(f"Column '{c}' cannot be empty", status_code=400)
        values[c] = cast_value(raw, data_types[c])

    values["id"] = id_int

    if update_cols:
        set_clause = ", ".join(f'"{c}" = :{c}' for c in update_cols)
        await database.execute(
            f'UPDATE "{SCHEMA_NAME}"."{table}" SET {set_clause} WHERE "{columns[0]}" = :id',
            values=values
        )

    return await render_table(request, selected_table=table, selected_row_id=id_int)

# ----------------------------
# Inline add form (INSERT)
# ----------------------------
@app.get("/crud_add_form", response_class=HTMLResponse)
async def crud_add_form(request: Request, selected_table: str):
    columns_raw = await get_table_columns(selected_table)
    columns = [c[0] for c in columns_raw]
    data_types = {c[0]: c[1] for c in columns_raw}
    column_lengths = {c[0]: c[3] for c in columns_raw}  # NEW

    # ✅ PK ALWAYS LOCKED
    locked_columns = {c: (c == columns[0] or c in ["updated_by", "updated_dt"]) for c in columns}

    required_columns = await get_required_columns(selected_table)

    show_columns = [c for c in columns if not locked_columns[c]]
    empty_row = {c: "" for c in show_columns}

    return templates.TemplateResponse(
        "crud_edit_form.html",
        {
            "request": request,
            "columns": show_columns,
            "data_types": data_types,
            "column_lengths": column_lengths,  # PASS LENGTHS
            "locked_columns": locked_columns,
            "required_columns": required_columns,
            "row": empty_row,
            "selected_table": selected_table,
            "insert_mode": True
        }
    )

@app.post("/crud/insert", response_class=HTMLResponse)
async def insert_row(request: Request):
    form = await request.form()
    table = form.get("table")

    if not table:
        return HTMLResponse("Missing table", status_code=400)

    columns_raw = await get_table_columns(table)
    columns = [c[0] for c in columns_raw]
    data_types = {c[0]: c[1] for c in columns_raw}
    locked_columns = {c[0]: (c[0] == columns[0]) for c in columns_raw}
    required_columns = await get_required_columns(table)

    insert_cols = [c for c in columns if c in form and not locked_columns[c]]
    values = {}

    for c in insert_cols:
        raw = form[c]
        if c in required_columns and str(raw).strip() == "":
            return HTMLResponse(
                f"Column '{c}' cannot be empty",
                status_code=400
            )
        values[c] = cast_value(raw, data_types[c])

    try:
        if insert_cols:
            col_names = ",".join(f'"{c}"' for c in insert_cols)
            val_names = ",".join(f":{c}" for c in insert_cols)
            query = f'''
                INSERT INTO "{SCHEMA_NAME}"."{table}"
                ({col_names})
                VALUES ({val_names})
            '''
            await database.execute(query=query, values=values)
    except Exception as e:
        return HTMLResponse(str(e), status_code=500)

    return await render_table(request, selected_table=table)

@app.get("/audio_picker", response_class=HTMLResponse)
async def audio_picker(request: Request):
    rows = await database.fetch_all(
        'SELECT id, title, type, file_url FROM playvocal.audio_file WHERE active = TRUE ORDER BY created_dt DESC'
    )
    return templates.TemplateResponse("audio_picker.html", {"request": request, "files": rows})

@app.get("/menu", response_class=HTMLResponse)
async def menu_home(request: Request):
    print("MENU ROUTE HIT")
    return templates.TemplateResponse(
        "menu_base.html",
        {"request": request}
    )

@app.get("/menu/crud", response_class=HTMLResponse)
async def menu_crud(request: Request):
    return await crud_index(request)

DIRECTIONS = ["NORTH", "SOUTH", "EAST", "WEST", "UP", "DOWN", "IN", "OUT"]

async def get_apps_by_type(app_type: str):
    query = """
        SELECT id, name
        FROM playvocal.app
        WHERE type = :type AND active = TRUE
        ORDER BY name
    """
    rows = await database.fetch_all(query=query, values={"type": app_type})
    return [dict(r) for r in rows]

@app.post("/app/create", response_class=HTMLResponse)
async def app_create(
    request: Request,
    type: str = Form(...),
    name: str = Form(...),
    default_greeting: str = Form(""),
    description: str = Form("")
):
    # QUESTION apps are created from the generic Admin/CRUD tab now, not
    # here -- one question per app means creating one is just a single
    # flat `app` row, nothing the generic CRUD grid doesn't already do
    # fine. type comes from a hidden field set by whichever screen's
    # "+ New App" form submitted, never a user-chosen dropdown, so this
    # whitelist is guarding against a malformed request, not real
    # user choice.
    if type not in ("STORY", "OBJLOC"):
        return HTMLResponse("Invalid app type", status_code=400)
    if not name.strip():
        return HTMLResponse("App name is required", status_code=400)

    row = await database.fetch_one(
        """
        INSERT INTO playvocal.app (name, type, default_greeting, description)
        VALUES (:name, :type, :greeting, :description)
        RETURNING id
        """,
        {
            "name": name.strip(),
            "type": type,
            "greeting": default_greeting.strip() or None,
            "description": description.strip() or None
        }
    )
    new_app_id = row["id"]

    if type == "STORY":
        return await render_story_screen(request, new_app_id)
    else:
        return await render_objloc_screen(request, new_app_id, room_id=None)

# ============================================================
# QUESTION editor ("Multiple Choice" tab)
# ============================================================
def _parse_json_field(value, default):
    """
    JSONB columns (answer_extra_fields, metadata) come back from asyncpg
    as either an already-decoded list/dict or a raw JSON string
    depending on driver configuration -- handle both rather than assume.
    """
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return default

async def get_question_nodes(app_id: int):
    # No active=TRUE filter here on purpose -- unlike the live call flow
    # (which only ever serves active questions), the editor has to show
    # disabled ones too, or there'd be no way to find and re-enable one
    # after turning it off.
    rows = await database.fetch_all(
        """
        SELECT * FROM playvocal.node
        WHERE app_id = :app_id
        ORDER BY order_index, id
        """,
        {"app_id": app_id}
    )
    return [dict(r) for r in rows]

async def get_answers(question_id: int):
    rows = await database.fetch_all(
        'SELECT * FROM playvocal.answer WHERE question_id = :qid ORDER BY id',
        {"qid": question_id}
    )
    answers = [dict(r) for r in rows]
    for a in answers:
        a["metadata"] = _parse_json_field(a.get("metadata"), {})
    return answers

async def render_question_screen(request: Request, app_id: int | None, question_id: int | None = None):
    apps = await get_apps_by_type("QUESTION")
    if app_id is None and apps:
        app_id = apps[0]["id"]

    questions = []
    selected_question = None
    selected_app = None
    if app_id:
        selected_app = dict(await database.fetch_one(
            'SELECT id, question_prompt, answer_extra_fields FROM playvocal.app WHERE id = :id', {"id": app_id}
        ))
        selected_app["answer_extra_fields"] = _parse_json_field(selected_app.get("answer_extra_fields"), [])
        questions = await get_question_nodes(app_id)
        for q in questions:
            q["answers"] = await get_answers(q["id"])
        if question_id:
            selected_question = next((q for q in questions if q["id"] == question_id), None)

    return templates.TemplateResponse("question_screen.html", {
        "request": request,
        "apps": apps,
        "selected_app_id": app_id,
        "selected_app": selected_app,
        "questions": questions,
        "selected_question": selected_question
    })

@app.get("/menu/question", response_class=HTMLResponse)
async def menu_question(request: Request):
    return await render_question_screen(request, app_id=None)

@app.get("/question/select", response_class=HTMLResponse)
async def question_select(request: Request, app_id: int = Query(...)):
    # Also used as the "back to list" target -- selecting an app (or
    # returning to one already selected) always lands on the list view,
    # never a stale drilled-in question.
    return await render_question_screen(request, app_id)

@app.get("/question/view", response_class=HTMLResponse)
async def question_view(request: Request, app_id: int = Query(...), question_id: int = Query(...)):
    return await render_question_screen(request, app_id, question_id)

@app.post("/question/app/edit", response_class=HTMLResponse)
async def question_app_edit(
    request: Request,
    app_id: int = Form(...),
    question_prompt: str = Form(""),
    answer_extra_fields: str = Form("")
):
    # question_prompt: the one prompt asked before every question in
    # this app ("Name this song?") -- set once here instead of retyped
    # per question. answer_extra_fields: comma-separated labels (e.g.
    # "Artist" or "Year, Director") for whatever extra attributes THIS
    # game's answers carry -- drives which inputs the answer editor
    # below shows, and the keys used in each answer's own metadata.
    fields = [f.strip() for f in answer_extra_fields.split(",") if f.strip()]
    await database.execute(
        """
        UPDATE playvocal.app
        SET question_prompt = :prompt, answer_extra_fields = :fields::jsonb,
            updated_by = 'SVX', updated_dt = NOW()
        WHERE id = :id
        """,
        {"prompt": question_prompt.strip() or None, "fields": json.dumps(fields), "id": app_id}
    )
    return await render_question_screen(request, app_id)

@app.post("/question/node/add", response_class=HTMLResponse)
async def question_node_add(
    request: Request,
    app_id: int = Form(...),
    audio_file_url: str = Form("")
):
    # No prompt_text here -- the app-level question_prompt ("Name this
    # song?") is asked before every question in this app, so there's
    # nothing per-question left to author except the clip itself; the
    # answers (added next) are what actually identify this round.
    row = await database.fetch_one(
        """
        INSERT INTO playvocal.node (parent_node_id, node_key, app_id, audio_file_url, order_index)
        VALUES (0, :node_key, :app_id, :audio_file_url,
                (SELECT COALESCE(MAX(order_index), -1) + 1 FROM playvocal.node WHERE app_id = :app_id))
        RETURNING id
        """,
        {
            "node_key": f"q_{uuid4().hex[:8]}",
            "app_id": app_id,
            "audio_file_url": audio_file_url.strip() or None
        }
    )
    # Drop straight into the new question's detail view (same "create
    # then immediately edit" flow as "+ New App") instead of back to the
    # list, since adding answers is the very next thing anyone will do.
    return await render_question_screen(request, app_id, question_id=row["id"])

@app.post("/question/node/edit", response_class=HTMLResponse)
async def question_node_edit(
    request: Request,
    app_id: int = Form(...),
    node_id: int = Form(...),
    audio_file_url: str | None = Form(None),
    active: str | None = Form(None)
):
    # audio_file_url and active are independently optional -- a quick
    # Active toggle from the list view only submits app_id/node_id/active
    # and must not silently blank out an already-set audio_file_url that
    # wasn't part of that particular request.
    set_clauses = []
    values = {"node_id": node_id}
    if audio_file_url is not None:
        set_clauses.append("audio_file_url = :audio_file_url")
        values["audio_file_url"] = audio_file_url.strip() or None
    if active is not None:
        set_clauses.append("active = :active")
        values["active"] = active.lower() in ("true", "1", "on")

    if set_clauses:
        set_clauses.append("updated_by = 'SVX'")
        set_clauses.append("updated_dt = NOW()")
        await database.execute(
            f"UPDATE playvocal.node SET {', '.join(set_clauses)} WHERE id = :node_id",
            values
        )
    return await render_question_screen(request, app_id, question_id=node_id)

@app.post("/question/node/delete", response_class=HTMLResponse)
async def question_node_delete(request: Request, app_id: int = Form(...), node_id: int = Form(...)):
    await database.execute('DELETE FROM playvocal.answer WHERE question_id = :id', {"id": node_id})
    await database.execute('DELETE FROM playvocal.node WHERE id = :id', {"id": node_id})
    return await render_question_screen(request, app_id)

def _extract_metadata_fields(form) -> dict:
    """
    Dynamic extra-field inputs are named "field:{label}" (e.g.
    "field:Artist", "field:Year") -- however many an app's
    answer_extra_fields defines, none of which are known at route-
    definition time, so they're pulled from the raw form data rather
    than declared as fixed Form(...) params.
    """
    metadata = {}
    for key, value in form.items():
        if key.startswith("field:") and str(value).strip():
            metadata[key[len("field:"):]] = str(value).strip()
    return metadata

@app.post("/question/answer/add", response_class=HTMLResponse)
async def question_answer_add(
    request: Request,
    app_id: int = Form(...),
    question_id: int = Form(...),
    answer_text: str = Form(...),
    is_correct: str = Form("false")
):
    metadata = _extract_metadata_fields(await request.form())
    row = await database.fetch_one(
        """
        INSERT INTO playvocal.answer (question_id, answer_text, metadata, is_correct)
        VALUES (:qid, :text, :metadata::jsonb, FALSE)
        RETURNING id
        """,
        {"qid": question_id, "text": answer_text.strip(), "metadata": json.dumps(metadata)}
    )
    if is_correct.lower() in ("true", "1", "on"):
        # (id = :answer_id) is the same single-statement pattern used by
        # /question/answer/set_correct -- it's what makes "more than one
        # correct answer per question" structurally impossible, not just
        # discouraged by the UI.
        await database.execute(
            'UPDATE playvocal.answer SET is_correct = (id = :answer_id) WHERE question_id = :qid',
            {"answer_id": row["id"], "qid": question_id}
        )
    return await render_question_screen(request, app_id, question_id=question_id)

@app.post("/question/answer/edit", response_class=HTMLResponse)
async def question_answer_edit(
    request: Request,
    app_id: int = Form(...),
    question_id: int = Form(...),
    answer_id: int = Form(...),
    answer_text: str = Form(...)
):
    metadata = _extract_metadata_fields(await request.form())
    await database.execute(
        """
        UPDATE playvocal.answer
        SET answer_text = :text, metadata = :metadata::jsonb, updated_by = 'SVX', updated_dt = NOW()
        WHERE id = :id
        """,
        {"text": answer_text.strip(), "metadata": json.dumps(metadata), "id": answer_id}
    )
    return await render_question_screen(request, app_id, question_id=question_id)

@app.post("/question/answer/set_correct", response_class=HTMLResponse)
async def question_answer_set_correct(
    request: Request,
    app_id: int = Form(...),
    question_id: int = Form(...),
    answer_id: int = Form(...)
):
    await database.execute(
        """
        UPDATE playvocal.answer
        SET is_correct = (id = :answer_id), updated_by = 'SVX', updated_dt = NOW()
        WHERE question_id = :qid
        """,
        {"answer_id": answer_id, "qid": question_id}
    )
    return await render_question_screen(request, app_id, question_id=question_id)

@app.post("/question/answer/delete", response_class=HTMLResponse)
async def question_answer_delete(
    request: Request,
    app_id: int = Form(...),
    question_id: int = Form(...),
    answer_id: int = Form(...)
):
    await database.execute('DELETE FROM playvocal.answer WHERE id = :id', {"id": answer_id})
    return await render_question_screen(request, app_id, question_id=question_id)

# ============================================================
# STORY editor ("Story" tab)
# ============================================================
async def get_story_nodes(app_id: int):
    rows = await database.fetch_all(
        """
        SELECT * FROM playvocal.node
        WHERE app_id = :app_id AND active = TRUE
        ORDER BY parent_node_id, order_index, id
        """,
        {"app_id": app_id}
    )
    return [dict(r) for r in rows]

def build_story_tree(nodes):
    """
    Builds both shapes from one walk over the same node dicts, since the
    screen needs both: `ordered` is a depth-annotated flat list (used for
    the parent-scene dropdowns, where a real tree widget would be
    overkill), `roots` is the top-level nodes with a real nested
    `children` list on every node (used to render the collapsible tree
    itself). A scene whose parent_node_id doesn't resolve to anything in
    this app (shouldn't happen through this editor) just becomes its own
    root rather than silently vanishing.
    """
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        n["children"] = []

    roots = []
    for n in nodes:
        parent = by_id.get(n["parent_node_id"])
        (parent["children"] if parent else roots).append(n)

    def sort_key(n):
        return (n["order_index"], n["id"])

    roots.sort(key=sort_key)
    for n in nodes:
        n["children"].sort(key=sort_key)

    ordered = []

    def walk(n, depth):
        n["depth"] = depth
        ordered.append(n)
        for c in n["children"]:
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)

    return ordered, roots

async def render_story_screen(request: Request, app_id: int | None, scene_id: int | None = None):
    apps = await get_apps_by_type("STORY")
    if app_id is None and apps:
        app_id = apps[0]["id"]

    scenes = []
    scene_tree = []
    selected_scene = None
    if app_id:
        scenes, scene_tree = build_story_tree(await get_story_nodes(app_id))
        if scene_id:
            selected_scene = next((s for s in scenes if s["id"] == scene_id), None)

    return templates.TemplateResponse("story_screen.html", {
        "request": request,
        "apps": apps,
        "selected_app_id": app_id,
        "scenes": scenes,
        "scene_tree": scene_tree,
        "selected_scene": selected_scene
    })

@app.get("/menu/story", response_class=HTMLResponse)
async def menu_story(request: Request):
    return await render_story_screen(request, app_id=None)

@app.get("/story/select", response_class=HTMLResponse)
async def story_select(request: Request, app_id: int = Query(...)):
    # Also used as the "back to list" target, same as /question/select.
    return await render_story_screen(request, app_id)

@app.get("/story/view", response_class=HTMLResponse)
async def story_view(request: Request, app_id: int = Query(...), scene_id: int = Query(...)):
    return await render_story_screen(request, app_id, scene_id)

@app.post("/story/node/add", response_class=HTMLResponse)
async def story_node_add(
    request: Request,
    app_id: int = Form(...),
    parent_node_id: int = Form(0),
    prompt_key: str = Form(""),
    prompt_text: str = Form(""),
    audio_file_url: str = Form("")
):
    # prompt_text (the scene's own narration) is optional here on purpose:
    # the natural way to build a branch is to stub out all of a scene's
    # option labels first ("grab the sword", "run home", "find a torch")
    # and fill in each one's actual narration later by opening it -- not
    # forced to write full narration before you're even allowed to note
    # the option exists.
    row = await database.fetch_one(
        """
        INSERT INTO playvocal.node (parent_node_id, node_key, app_id, prompt_text, audio_file_url, prompt_key, order_index)
        VALUES (:parent_node_id, :node_key, :app_id, :prompt_text, :audio_file_url, :prompt_key,
                (SELECT COALESCE(MAX(order_index), -1) + 1 FROM playvocal.node
                 WHERE app_id = :app_id AND parent_node_id = :parent_node_id))
        RETURNING id
        """,
        {
            "parent_node_id": parent_node_id,
            "node_key": f"scene_{uuid4().hex[:8]}",
            "app_id": app_id,
            "prompt_text": prompt_text.strip() or None,
            "audio_file_url": audio_file_url.strip() or None,
            "prompt_key": prompt_key.strip() or None
        }
    )
    # Land back on the PARENT's own detail view (or the tree list, if this
    # was a new top-level scene) -- adding option 1, then 2, then 3 under
    # the same scene shouldn't require re-navigating in between each one.
    # To open the option just created instead, click it from that list.
    return await render_story_screen(request, app_id, scene_id=(parent_node_id or None))

@app.post("/story/node/edit", response_class=HTMLResponse)
async def story_node_edit(
    request: Request,
    app_id: int = Form(...),
    node_id: int = Form(...),
    parent_node_id: int = Form(0),
    prompt_key: str = Form(""),
    prompt_text: str = Form(""),
    audio_file_url: str = Form("")
):
    await database.execute(
        """
        UPDATE playvocal.node
        SET parent_node_id = :parent_node_id, prompt_key = :prompt_key, prompt_text = :prompt_text,
            audio_file_url = :audio_file_url, updated_by = 'SVX', updated_dt = NOW()
        WHERE id = :node_id
        """,
        {
            "parent_node_id": parent_node_id,
            "prompt_key": prompt_key.strip() or None,
            "prompt_text": prompt_text.strip() or None,
            "audio_file_url": audio_file_url.strip() or None,
            "node_id": node_id
        }
    )
    return await render_story_screen(request, app_id, scene_id=node_id)

@app.post("/story/node/delete", response_class=HTMLResponse)
async def story_node_delete(request: Request, app_id: int = Form(...), node_id: int = Form(...)):
    # Re-parent this scene's own children up to its parent instead of
    # cascading the delete through the whole subtree -- deleting one scene
    # shouldn't silently erase every scene branching from it.
    row = await database.fetch_one('SELECT parent_node_id FROM playvocal.node WHERE id = :id', {"id": node_id})
    parent_of_deleted = row["parent_node_id"] if row else 0
    await database.execute(
        'UPDATE playvocal.node SET parent_node_id = :new_parent WHERE parent_node_id = :old_parent',
        {"new_parent": parent_of_deleted, "old_parent": node_id}
    )
    await database.execute('DELETE FROM playvocal.node WHERE id = :id', {"id": node_id})
    return await render_story_screen(request, app_id)

# ============================================================
# OBJLOC editor ("Worlds" tab)
# ============================================================
async def get_rooms(app_id: int):
    rows = await database.fetch_all(
        """
        SELECT * FROM playvocal.node
        WHERE app_id = :app_id AND active = TRUE
        ORDER BY order_index, id
        """,
        {"app_id": app_id}
    )
    return [dict(r) for r in rows]

async def get_exits(room_id: int):
    rows = await database.fetch_all(
        """
        SELECT * FROM playvocal.node_connection
        WHERE from_node_id = :room_id AND active = TRUE
        ORDER BY direction
        """,
        {"room_id": room_id}
    )
    return [dict(r) for r in rows]

async def get_room_objects(room_id: int):
    rows = await database.fetch_all(
        """
        SELECT * FROM playvocal.object
        WHERE start_node_id = :room_id AND active = TRUE
        ORDER BY id
        """,
        {"room_id": room_id}
    )
    return [dict(r) for r in rows]

async def render_objloc_screen(request: Request, app_id: int | None, room_id: int | None):
    apps = await get_apps_by_type("OBJLOC")
    if app_id is None and apps:
        app_id = apps[0]["id"]

    rooms = []
    exits = []
    room_objects = []
    if app_id:
        rooms = await get_rooms(app_id)
        if room_id is None and rooms:
            room_id = rooms[0]["id"]
        elif room_id is not None and not any(r["id"] == room_id for r in rooms):
            room_id = rooms[0]["id"] if rooms else None
        if room_id:
            exits = await get_exits(room_id)
            room_objects = await get_room_objects(room_id)

    return templates.TemplateResponse("objloc_screen.html", {
        "request": request,
        "apps": apps,
        "selected_app_id": app_id,
        "rooms": rooms,
        "selected_room_id": room_id,
        "exits": exits,
        "room_objects": room_objects,
        "directions": DIRECTIONS
    })

@app.get("/menu/objloc", response_class=HTMLResponse)
async def menu_objloc(request: Request):
    return await render_objloc_screen(request, app_id=None, room_id=None)

@app.get("/objloc/select", response_class=HTMLResponse)
async def objloc_select(request: Request, app_id: int = Query(...), room_id: int | None = Query(None)):
    return await render_objloc_screen(request, app_id, room_id)

@app.post("/objloc/room/add", response_class=HTMLResponse)
async def objloc_room_add(
    request: Request,
    app_id: int = Form(...),
    prompt_text: str = Form(...),
    audio_file_url: str = Form(""),
    description: str = Form("")
):
    row = await database.fetch_one(
        """
        INSERT INTO playvocal.node (parent_node_id, node_key, app_id, prompt_text, audio_file_url, description, order_index)
        VALUES (0, :node_key, :app_id, :prompt_text, :audio_file_url, :description,
                (SELECT COALESCE(MAX(order_index), -1) + 1 FROM playvocal.node WHERE app_id = :app_id))
        RETURNING id
        """,
        {
            "node_key": f"room_{uuid4().hex[:8]}",
            "app_id": app_id,
            "prompt_text": prompt_text.strip(),
            "audio_file_url": audio_file_url.strip() or None,
            "description": description.strip() or None
        }
    )
    return await render_objloc_screen(request, app_id, room_id=row["id"])

@app.post("/objloc/room/edit", response_class=HTMLResponse)
async def objloc_room_edit(
    request: Request,
    app_id: int = Form(...),
    room_id: int = Form(...),
    prompt_text: str = Form(...),
    audio_file_url: str = Form(""),
    description: str = Form("")
):
    await database.execute(
        """
        UPDATE playvocal.node
        SET prompt_text = :prompt_text, audio_file_url = :audio_file_url, description = :description,
            updated_by = 'SVX', updated_dt = NOW()
        WHERE id = :room_id
        """,
        {
            "prompt_text": prompt_text.strip(),
            "audio_file_url": audio_file_url.strip() or None,
            "description": description.strip() or None,
            "room_id": room_id
        }
    )
    return await render_objloc_screen(request, app_id, room_id)

@app.post("/objloc/room/delete", response_class=HTMLResponse)
async def objloc_room_delete(request: Request, app_id: int = Form(...), room_id: int = Form(...)):
    await database.execute(
        'DELETE FROM playvocal.node_connection WHERE from_node_id = :id OR to_node_id = :id',
        {"id": room_id}
    )
    await database.execute('DELETE FROM playvocal.object WHERE start_node_id = :id', {"id": room_id})
    await database.execute('DELETE FROM playvocal.node WHERE id = :id', {"id": room_id})
    return await render_objloc_screen(request, app_id, room_id=None)

@app.post("/objloc/connection/add", response_class=HTMLResponse)
async def objloc_connection_add(
    request: Request,
    app_id: int = Form(...),
    room_id: int = Form(...),
    direction: str = Form(...),
    to_node_id: int = Form(...)
):
    if direction not in DIRECTIONS:
        return HTMLResponse("Invalid direction", status_code=400)
    await database.execute(
        'INSERT INTO playvocal.node_connection (from_node_id, direction, to_node_id) VALUES (:from_id, :direction, :to_id)',
        {"from_id": room_id, "direction": direction, "to_id": to_node_id}
    )
    return await render_objloc_screen(request, app_id, room_id)

@app.post("/objloc/connection/edit", response_class=HTMLResponse)
async def objloc_connection_edit(
    request: Request,
    app_id: int = Form(...),
    room_id: int = Form(...),
    connection_id: int = Form(...),
    direction: str = Form(...),
    to_node_id: int = Form(...)
):
    if direction not in DIRECTIONS:
        return HTMLResponse("Invalid direction", status_code=400)
    await database.execute(
        'UPDATE playvocal.node_connection SET direction = :direction, to_node_id = :to_id WHERE id = :id',
        {"direction": direction, "to_id": to_node_id, "id": connection_id}
    )
    return await render_objloc_screen(request, app_id, room_id)

@app.post("/objloc/connection/delete", response_class=HTMLResponse)
async def objloc_connection_delete(request: Request, app_id: int = Form(...), room_id: int = Form(...), connection_id: int = Form(...)):
    await database.execute('DELETE FROM playvocal.node_connection WHERE id = :id', {"id": connection_id})
    return await render_objloc_screen(request, app_id, room_id)

@app.post("/objloc/object/add", response_class=HTMLResponse)
async def objloc_object_add(
    request: Request,
    app_id: int = Form(...),
    room_id: int = Form(...),
    object_key: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    takeable: str = Form("false")
):
    await database.execute(
        """
        INSERT INTO playvocal.object
        (app_id, object_key, name, description, start_node_id, current_location_id, takeable)
        VALUES (:app_id, :object_key, :name, :description, :room_id, :room_id, :takeable)
        """,
        {
            "app_id": app_id,
            "object_key": object_key.strip(),
            "name": name.strip(),
            "description": description.strip() or None,
            "room_id": room_id,
            "takeable": takeable.lower() in ("true", "1", "on")
        }
    )
    return await render_objloc_screen(request, app_id, room_id)

@app.post("/objloc/object/edit", response_class=HTMLResponse)
async def objloc_object_edit(
    request: Request,
    app_id: int = Form(...),
    room_id: int = Form(...),
    object_id: int = Form(...),
    object_key: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    takeable: str = Form("false")
):
    # Deliberately does not touch start_node_id/current_location_id --
    # those are live runtime state mutated by the running app itself
    # (see config.CONTENT_CACHE_TTL's comment in the backend). A content
    # edit here must never teleport an object that's already in play.
    await database.execute(
        """
        UPDATE playvocal.object
        SET object_key = :object_key, name = :name, description = :description, takeable = :takeable,
            updated_by = 'SVX', updated_dt = NOW()
        WHERE id = :object_id
        """,
        {
            "object_key": object_key.strip(),
            "name": name.strip(),
            "description": description.strip() or None,
            "takeable": takeable.lower() in ("true", "1", "on"),
            "object_id": object_id
        }
    )
    return await render_objloc_screen(request, app_id, room_id)

@app.post("/objloc/object/delete", response_class=HTMLResponse)
async def objloc_object_delete(request: Request, app_id: int = Form(...), room_id: int = Form(...), object_id: int = Form(...)):
    await database.execute('DELETE FROM playvocal.object WHERE id = :id', {"id": object_id})
    return await render_objloc_screen(request, app_id, room_id)

#
#  TWILIO Requirements 
#
@app.post("/twilio/fallback")
async def twilio_fallback():
    return Response(
        """
        <Response>
            <Say voice="Polly.Kendra-Neural" language="en-US">
                The system is unavailable. Please try again when you like.
            </Say>
            <Hangup/>
        </Response>
        """,
        media_type="application/xml",
    )

