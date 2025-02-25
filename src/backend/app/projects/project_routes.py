# Copyright (c) 2022, 2023 Humanitarian OpenStreetMap Team
#
# This file is part of FMTM.
#
#     FMTM is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.
#
#     FMTM is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with FMTM.  If not, see <https:#www.gnu.org/licenses/>.
#
"""Endpoints for FMTM projects."""

import json
import os
import uuid
from pathlib import Path
from typing import Optional

import geojson
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger as log
from osm_fieldwork.make_data_extract import getChoices
from osm_fieldwork.xlsforms import xlsforms_path
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from app.auth.osm import AuthUser, login_required
from app.central import central_crud
from app.db import database, db_models
from app.models.enums import TILES_FORMATS, TILES_SOURCE, HTTPStatus
from app.projects import project_crud, project_deps, project_schemas
from app.projects.project_crud import check_crs
from app.static import data_path
from app.submission import submission_crud
from app.tasks import tasks_crud

router = APIRouter(
    prefix="/projects",
    tags=["projects"],
    dependencies=[Depends(database.get_db)],
    responses={404: {"description": "Not found"}},
)


@router.get("/", response_model=list[project_schemas.ProjectOut])
async def read_projects(
    user_id: int = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(database.get_db),
):
    """Return all projects."""
    project_count, projects = await project_crud.get_projects(db, user_id, skip, limit)
    return projects


@router.get("/details/{project_id}/")
async def get_projet_details(project_id: int, db: Session = Depends(database.get_db)):
    """Returns the project details.

    Also includes ODK project details, so takes extra time to return.

    Parameters:
        project_id: int

    Returns:
        Response: Project details.
    """
    project = await project_crud.get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, details={"Project not found"})

    # ODK Credentials
    odk_credentials = project_schemas.ODKCentral(
        odk_central_url=project.odk_central_url,
        odk_central_user=project.odk_central_user,
        odk_central_password=project.odk_central_password,
    )

    odk_details = central_crud.get_odk_project_full_details(
        project.odkid, odk_credentials
    )

    # Features count
    query = text(
        "select count(*) from features where "
        f"project_id={project_id} and task_id is not null"
    )
    result = db.execute(query)
    features = result.fetchone()[0]

    return {
        "id": project_id,
        "odkName": odk_details["name"],
        "createdAt": odk_details["createdAt"],
        "tasks": odk_details["forms"],
        "lastSubmission": odk_details["lastSubmission"],
        "total_features": features,
    }


@router.post("/near_me", response_model=list[project_schemas.ProjectSummary])
async def get_tasks_near_me(lat: float, long: float, user_id: int = None):
    """Get projects near me.

    TODO to be implemented in future.
    """
    return [project_schemas.ProjectSummary()]


@router.get("/summaries", response_model=project_schemas.PaginatedProjectSummaries)
async def read_project_summaries(
    user_id: int = None,
    hashtags: str = None,
    page: int = Query(1, ge=1),  # Default to page 1, must be greater than or equal to 1
    results_per_page: int = Query(13, le=100),
    db: Session = Depends(database.get_db),
):
    """Get a paginated summary of projects."""
    if hashtags:
        hashtags = hashtags.split(",")  # create list of hashtags
        hashtags = list(
            filter(lambda hashtag: hashtag.startswith("#"), hashtags)
        )  # filter hashtags that do start with #

    total_projects = db.query(db_models.DbProject).count()
    skip = (page - 1) * results_per_page
    limit = results_per_page

    project_count, projects = await project_crud.get_project_summaries(
        db, user_id, skip, limit, hashtags, None
    )

    pagination = await project_crud.get_pagination(
        page, project_count, results_per_page, total_projects
    )
    project_summaries = [
        project_schemas.ProjectSummary.from_db_project(project) for project in projects
    ]

    response = project_schemas.PaginatedProjectSummaries(
        results=project_summaries,
        pagination=pagination,
    )
    return response


@router.get(
    "/search_projects", response_model=project_schemas.PaginatedProjectSummaries
)
async def search_project(
    search: str,
    user_id: int = None,
    hashtags: str = None,
    page: int = Query(1, ge=1),  # Default to page 1, must be greater than or equal to 1
    results_per_page: int = Query(13, le=100),
    db: Session = Depends(database.get_db),
):
    """Search projects by string, hashtag, or other criteria."""
    if hashtags:
        hashtags = hashtags.split(",")  # create list of hashtags
        hashtags = list(
            filter(lambda hashtag: hashtag.startswith("#"), hashtags)
        )  # filter hashtags that do start with #

    total_projects = db.query(db_models.DbProject).count()
    skip = (page - 1) * results_per_page
    limit = results_per_page

    project_count, projects = await project_crud.get_project_summaries(
        db, user_id, skip, limit, hashtags, search
    )

    pagination = await project_crud.get_pagination(
        page, project_count, results_per_page, total_projects
    )
    project_summaries = [
        project_schemas.ProjectSummary.from_db_project(project) for project in projects
    ]

    response = project_schemas.PaginatedProjectSummaries(
        results=project_summaries,
        pagination=pagination,
    )
    return response


@router.get("/{project_id}", response_model=project_schemas.ReadProject)
async def read_project(project_id: int, db: Session = Depends(database.get_db)):
    """Get a specific project by ID."""
    project = await project_crud.get_project_by_id(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.delete("/{project_id}")
async def delete_project(
    project: int = Depends(project_deps.get_project_by_id),
    db: Session = Depends(database.get_db),
    user_data: AuthUser = Depends(login_required),
):
    """Delete a project from both ODK Central and the local database."""
    # Odk crendentials
    odk_credentials = project_schemas.ODKCentral(
        odk_central_url=project.odk_central_url,
        odk_central_user=project.odk_central_user,
        odk_central_password=project.odk_central_password,
    )
    # Delete ODK Central project
    await central_crud.delete_odk_project(project.odkid, odk_credentials)
    # Delete FMTM project
    await project_crud.delete_one_project(db, project)
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.post("/create_project", response_model=project_schemas.ProjectOut)
async def create_project(
    project_info: project_schemas.ProjectUpload,
    db: Session = Depends(database.get_db),
):
    """Create a project in ODK Central and the local database.

    TODO refactor to standard REST POST to /projects
    TODO but first check doesn't break other endpoints
    """
    log.debug(f"Creating project {project_info.project_info.name}")

    if project_info.odk_central.odk_central_url.endswith("/"):
        project_info.odk_central.odk_central_url = (
            project_info.odk_central.odk_central_url[:-1]
        )

    odkproject = central_crud.create_odk_project(
        project_info.project_info.name, project_info.odk_central
    )

    # TODO check token against user or use token instead of passing user
    # project_info.project_name_prefix = project_info.project_info.name
    project = await project_crud.create_project_with_project_info(
        db, project_info, odkproject["id"]
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project creation failed")

    return project


@router.put("/{id}", response_model=project_schemas.ProjectOut)
async def update_project(
    id: int,
    project_info: project_schemas.ProjectUpload,
    db: Session = Depends(database.get_db),
):
    """Update an existing project by ID.

    Note: the entire project JSON must be uploaded.
    If a partial update is required, use the PATCH method instead.

    Parameters:
    - id: ID of the project to update
    - author: Author username and id
    - project_info: Updated project information

    Returns:
    - Updated project information

    Raises:
    - HTTPException with 404 status code if project not found
    """
    project = await project_crud.update_project_info(db, project_info, id)
    if not project:
        raise HTTPException(status_code=422, detail="Project could not be updated")
    return project


@router.patch("/{id}", response_model=project_schemas.ProjectOut)
async def project_partial_update(
    id: int,
    project_info: project_schemas.ProjectUpdate,
    db: Session = Depends(database.get_db),
):
    """Partial Update an existing project by ID.

    Parameters:
    - id
    - name
    - short_description
    - description

    Returns:
    - Updated project information

    Raises:
    - HTTPException with 404 status code if project not found
    """
    # Update project informations
    project = await project_crud.partial_update_project_info(db, project_info, id)

    if not project:
        raise HTTPException(status_code=422, detail="Project could not be updated")
    return project


@router.post("/upload_xlsform")
async def upload_custom_xls(
    upload: UploadFile = File(...),
    category: str = Form(...),
    db: Session = Depends(database.get_db),
):
    """Upload a custom XLSForm to the database.

    Args:
        upload (UploadFile): the XLSForm file
        category (str): the category of the XLSForm.
        db (Session): the DB session, provided automatically.
    """
    content = await upload.read()  # read file content
    name = upload.filename.split(".")[0]  # get name of file without extension
    await project_crud.upload_xlsform(db, content, name, category)

    # FIXME: fix return value
    return {"xform_title": f"{category}"}


@router.post("/{project_id}/custom_task_boundaries")
async def upload_custom_task_boundaries(
    project_id: int,
    project_geojson: UploadFile = File(...),
    db: Session = Depends(database.get_db),
):
    """Set project task boundaries manually using multi-polygon GeoJSON.

    Each polygon in the uploaded geojson are made a single task.

    Required Parameters:
        project_id (id): ID for associated project.
        project_geojson (UploadFile): Multi-polygon GeoJSON file.

    Returns:
        dict: JSON containing success message, project ID, and number of tasks.
    """
    log.debug(f"Uploading project boundary multipolygon for project ID: {project_id}")
    # read entire file
    content = await project_geojson.read()
    boundary = json.loads(content)

    # Validatiing Coordinate Reference System
    check_crs(boundary)

    log.debug("Creating tasks for each polygon in project")
    result = await project_crud.update_multi_polygon_project_boundary(
        db, project_id, boundary
    )

    if not result:
        raise HTTPException(
            status_code=428, detail=f"Project with id {project_id} does not exist"
        )

    # Get the number of tasks in a project
    task_count = await tasks_crud.get_task_count_in_project(db, project_id)

    return {
        "message": "Project Boundary Uploaded",
        "project_id": f"{project_id}",
        "task_count": task_count,
    }


@router.post("/task_split")
async def task_split(
    project_geojson: UploadFile = File(...),
    extract_geojson: UploadFile = File(...),
    no_of_buildings: int = Form(50),
    db: Session = Depends(database.get_db),
):
    """Split a task into subtasks.

    Args:
        project_geojson (UploadFile): The geojson to split.
            Should be a FeatureCollection.
        extract_geojson (UploadFile): Data extract geojson containing osm features.
            Should be a FeatureCollection.
        no_of_buildings (int, optional): The number of buildings per subtask.
            Defaults to 50.
        db (Session, optional): The database session. Injected by FastAPI.

    Returns:
        The result of splitting the task into subtasks.

    """
    # read project boundary
    parsed_boundary = geojson.loads(await project_geojson.read())
    # Validatiing Coordinate Reference Systems
    check_crs(parsed_boundary)

    # read data extract
    parsed_extract = geojson.loads(await extract_geojson.read())

    check_crs(parsed_extract)

    return await project_crud.split_geojson_into_tasks(
        db,
        parsed_boundary,
        parsed_extract,
        no_of_buildings,
    )


@router.post("/{project_id}/upload")
async def upload_project_boundary(
    project_id: int,
    boundary_geojson: UploadFile = File(...),
    dimension: int = Form(500),
    db: Session = Depends(database.get_db),
):
    """Uploads the project boundary. The boundary is uploaded as a geojson file.

    Args:
        project_id (int): The ID of the project to update.
        boundary_geojson (UploadFile): The boundary file to upload.
        dimension (int): The new dimension of the project.
        db (Session): The database session to use.

    Returns:
        dict: JSON with message, project ID, and task count for project.
    """
    # Validating for .geojson File.
    file_name = os.path.splitext(boundary_geojson.filename)
    file_ext = file_name[1]
    allowed_extensions = [".geojson", ".json"]
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Provide a valid .geojson file")

    # read entire file
    content = await boundary_geojson.read()
    boundary = json.loads(content)

    # Validatiing Coordinate Reference System
    check_crs(boundary)

    # update project boundary and dimension
    result = await project_crud.update_project_boundary(
        db, project_id, boundary, dimension
    )
    if not result:
        raise HTTPException(
            status_code=428, detail=f"Project with id {project_id} does not exist"
        )

    # Get the number of tasks in a project
    task_count = await tasks_crud.get_task_count_in_project(db, project_id)

    return {
        "message": "Project Boundary Uploaded",
        "project_id": project_id,
        "task_count": task_count,
    }


@router.post("/edit_project_boundary/{project_id}/")
async def edit_project_boundary(
    project_id: int,
    boundary_geojson: UploadFile = File(...),
    dimension: int = Form(500),
    db: Session = Depends(database.get_db),
):
    """Edit the existing project boundary."""
    # Validating for .geojson File.
    file_name = os.path.splitext(boundary_geojson.filename)
    file_ext = file_name[1]
    allowed_extensions = [".geojson", ".json"]
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Provide a valid .geojson file")

    # read entire file
    content = await boundary_geojson.read()
    boundary = json.loads(content)

    # Validatiing Coordinate Reference System
    check_crs(boundary)

    result = await project_crud.update_project_boundary(
        db, project_id, boundary, dimension
    )
    if not result:
        raise HTTPException(
            status_code=428, detail=f"Project with id {project_id} does not exist"
        )

    # Get the number of tasks in a project
    task_count = await tasks_crud.get_task_count_in_project(db, project_id)

    return {
        "message": "Project Boundary Uploaded",
        "project_id": project_id,
        "task_count": task_count,
    }


@router.post("/validate_form")
async def validate_form(
    form: UploadFile,
):
    """Tests the validity of the xls form uploaded.

    Parameters:
        - form: The xls form to validate
    """
    file_name = os.path.splitext(form.filename)
    file_ext = file_name[1]

    allowed_extensions = [".xls", ".xlsx"]
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Provide a valid .xls file")

    contents = await form.read()
    return await central_crud.test_form_validity(contents, file_ext[1:])


@router.post("/{project_id}/generate")
async def generate_files(
    background_tasks: BackgroundTasks,
    project_id: int,
    extract_polygon: bool = Form(False),
    xls_form_upload: Optional[UploadFile] = File(None),
    xls_form_config_file: Optional[UploadFile] = File(None),
    data_extracts: Optional[UploadFile] = File(None),
    db: Session = Depends(database.get_db),
):
    """Generate additional content to initialise the project.

    Boundary, ODK Central forms, QR codes, etc.

    Accepts a project ID, category, custom form flag, and an uploaded file as inputs.
    The generated files are associated with the project ID and stored in the database.
    This api generates qr_code, forms. This api also creates an app user for
    each task and provides the required roles.
    Some of the other functionality of this api includes converting a xls file
    provided by the user to the xform, generates osm data extracts and uploads
    it to the form.

    Args:
        background_tasks (BackgroundTasks): FastAPI bg tasks, provided automatically.
        project_id (int): The ID of the project for which files are being generated.
        extract_polygon (bool): A boolean flag indicating whether the polygon
            is extracted or not.
        xls_form_upload (UploadFile, optional): A custom XLSForm to use in the project.
            A file should be provided if user wants to upload a custom xls form.
        xls_form_config_file (UploadFile, optional): The config YAML for the XLS form.
        data_extracts (UploadFile, optional): Custom data extract GeoJSON.
        db (Session): Database session, provided automatically.

    Returns:
        json (JSONResponse): A success message containing the project ID.
    """
    log.debug(f"Generating media files tasks for project: {project_id}")
    custom_xls_form = None
    xform_title = None

    project = await project_crud.get_project(db, project_id)
    if not project:
        raise HTTPException(
            status_code=428, detail=f"Project with id {project_id} does not exist"
        )

    project.data_extract_type = "polygon" if extract_polygon else "centroid"
    db.commit()

    if xls_form_upload:
        log.debug("Validating uploaded XLS form")
        # Validating for .XLS File.
        file_name = os.path.splitext(xls_form_upload.filename)
        file_ext = file_name[1]
        allowed_extensions = [".xls", ".xlsx", ".xml"]
        if file_ext not in allowed_extensions:
            raise HTTPException(status_code=400, detail="Provide a valid .xls file")
        xform_title = file_name[0]
        custom_xls_form = await xls_form_upload.read()

        project.form_xls = custom_xls_form

        if xls_form_config_file:
            config_file_name = os.path.splitext(xls_form_config_file.filename)
            config_file_ext = config_file_name[1]
            if not config_file_ext == ".yaml":
                raise HTTPException(
                    status_code=400, detail="Provide a valid .yaml config file"
                )
            config_file_contents = await xls_form_config_file.read()
            project.form_config_file = config_file_contents

        db.commit()

    if data_extracts:
        log.debug("Validating uploaded geojson file")
        # Validating for .geojson File.
        data_extracts_file_name = os.path.splitext(data_extracts.filename)
        extracts_file_ext = data_extracts_file_name[1]
        if extracts_file_ext != ".geojson":
            raise HTTPException(status_code=400, detail="Provide a valid geojson file")
        try:
            extracts_contents = await data_extracts.read()
            json.loads(extracts_contents)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400, detail="Provide a valid geojson file"
            ) from e

    # Create task in db and return uuid
    log.debug(f"Creating export background task for project ID: {project_id}")
    background_task_id = await project_crud.insert_background_task_into_database(
        db, project_id=project_id
    )

    log.debug(f"Submitting {background_task_id} to background tasks stack")
    background_tasks.add_task(
        project_crud.generate_appuser_files,
        db,
        project_id,
        extract_polygon,
        custom_xls_form,
        extracts_contents if data_extracts else None,
        xform_title,
        file_ext[1:] if xls_form_upload else "xls",
        background_task_id,
    )

    return JSONResponse(
        status_code=200,
        content={"Message": f"{project_id}", "task_id": f"{background_task_id}"},
    )


@router.post("/update-form/{project_id}")
async def update_project_form(
    project_id: int,
    form: Optional[UploadFile],
    db: Session = Depends(database.get_db),
):
    """Update XLSForm for a project."""
    file_name = os.path.splitext(form.filename)
    file_ext = file_name[1]
    allowed_extensions = [".xls"]
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Provide a valid .xls file")
    contents = await form.read()

    form_updated = await project_crud.update_project_form(
        db, project_id, contents, file_ext[1:]  # Form Contents  # File type
    )

    return form_updated


@router.get("/{project_id}/features", response_model=list[project_schemas.Feature])
async def get_project_features(
    project_id: int,
    task_id: int = None,
    db: Session = Depends(database.get_db),
):
    """Fetch all the features for a project.

    The features are generated from raw-data-api.

    Args:
        project_id (int): The project id.
        task_id (int): The task id.
        db (Session): the DB session, provided automatically.

    Returns:
        feature(json): JSON object containing a list of features
    """
    features = await project_crud.get_project_features(db, project_id, task_id)
    return features


@router.get("/generate-log/")
async def generate_log(
    project_id: int, uuid: uuid.UUID, db: Session = Depends(database.get_db)
):
    r"""Get the contents of a log file in a log format.

    ### Response
    - **200 OK**: Returns the contents of the log file in a log format.
        Each line is separated by a newline character "\n".

    - **500 Internal Server Error**: Returns an error message if the log file
        cannot be generated.

    ### Return format
    Task Status and Logs are returned in a JSON format.
    """
    try:
        # Get the backgrund task status
        task_status, task_message = await project_crud.get_background_task_status(
            uuid, db
        )
        extract_completion_count = (
            db.query(db_models.DbProject)
            .filter(db_models.DbProject.id == project_id)
            .first()
        ).extract_completed_count

        with open("/opt/logs/create_project.json", "r") as log_file:
            logs = [json.loads(line) for line in log_file]

            filtered_logs = [
                log.get("record", {}).get("message", None)
                for log in logs
                if log.get("record", {}).get("extra", {}).get("project_id")
                == project_id
            ]
            last_50_logs = filtered_logs[-50:]

            logs = "\n".join(last_50_logs)
            task_count = await project_crud.get_tasks_count(db, project_id)
            return {
                "status": task_status.name,
                "total_tasks": task_count,
                "message": task_message,
                "progress": extract_completion_count,
                "logs": logs,
            }
    except Exception as e:
        log.error(e)
        return "Error in generating log file"


@router.get("/categories/")
async def get_categories():
    """Get api for fetching all the categories.

    This endpoint fetches all the categories from osm_fieldwork.

    ## Response
    - Returns a JSON object containing a list of categories and their respoective forms.

    """
    # FIXME update to use osm-rawdata
    categories = (
        getChoices()
    )  # categories are fetched from osm_fieldwork.make_data_extracts.getChoices()
    return categories


@router.post("/preview_split_by_square/")
async def preview_split_by_square(
    project_geojson: UploadFile = File(...), dimension: int = Form(100)
):
    """Preview splitting by square.

    TODO update to use a response_model
    """
    # Validating for .geojson File.
    file_name = os.path.splitext(project_geojson.filename)
    file_ext = file_name[1]
    allowed_extensions = [".geojson", ".json"]
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Provide a valid .geojson file")

    # read entire file
    content = await project_geojson.read()
    boundary = geojson.loads(content)

    # Validatiing Coordinate Reference System
    check_crs(boundary)

    result = await project_crud.preview_split_by_square(boundary, dimension)
    return result


@router.post("/get_data_extract/")
async def get_data_extract(
    geojson_file: UploadFile = File(...),
    project_id: int = Query(None, description="Project ID"),
    db: Session = Depends(database.get_db),
):
    """Get the data extract for a given project AOI.

    Use for both generating a new data extract and for getting
    and existing extract.
    """
    boundary_geojson = json.loads(await geojson_file.read())

    fgb_url = await project_crud.get_data_extract_url(
        db,
        boundary_geojson,
        project_id,
    )
    return JSONResponse(status_code=200, content={"url": fgb_url})


@router.post("/upload_custom_extract/")
async def upload_custom_extract(
    background_tasks: BackgroundTasks,
    custom_extract_file: UploadFile = File(...),
    project_id: int = Query(..., description="Project ID"),
    db: Session = Depends(database.get_db),
):
    """Upload a custom data extract for a project as fgb in S3.

    Request Body
    - 'custom_extract_file' (file): Geojson files with the features. Required.

    Query Params:
    - 'project_id' (int): the project's id. Required.
    """
    # Validating for .geojson File.
    file_name = os.path.splitext(custom_extract_file.filename)
    file_ext = file_name[1]
    allowed_extensions = [".geojson", ".json"]
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Provide a valid .geojson file")

    # read entire file
    geojson_str = await custom_extract_file.read()

    log.debug("Creating upload_custom_extract background task")
    fgb_url = await project_crud.upload_custom_data_extract(db, project_id, geojson_str)
    return JSONResponse(status_code=200, content={"url": fgb_url})


@router.get("/download_form/{project_id}/")
async def download_form(project_id: int, db: Session = Depends(database.get_db)):
    """Download the XLSForm for a project."""
    project = await project_crud.get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    headers = {
        "Content-Disposition": "attachment; filename=submission_data.xls",
        "Content-Type": "application/media",
    }
    if not project.form_xls:
        project_category = project.xform_title
        xlsform_path = f"{xlsforms_path}/{project_category}.xls"
        if os.path.exists(xlsform_path):
            return FileResponse(xlsform_path, filename="form.xls")
        else:
            raise HTTPException(status_code=404, detail="Form not found")
    return Response(content=project.form_xls, headers=headers)


@router.post("/update_category")
async def update_project_category(
    # background_tasks: BackgroundTasks,
    project_id: int = Form(...),
    category: str = Form(...),
    upload: Optional[UploadFile] = File(None),
    db: Session = Depends(database.get_db),
):
    """Update the XLSForm category for a project.

    Not valid for custom form uploads.
    """
    contents = None

    project = await project_crud.get_project(db, project_id)
    if not project:
        raise HTTPException(
            status_code=400, detail=f"Project with id {project_id} does not exist"
        )

    current_category = project.xform_title
    if current_category == category:
        if not upload:
            raise HTTPException(
                status_code=400, detail="Current category is same as new category"
            )

    if upload:
        # Validating for .XLS File.
        file_name = os.path.splitext(upload.filename)
        file_ext = file_name[1]
        allowed_extensions = [".xls", ".xlsx", ".xml"]
        if file_ext not in allowed_extensions:
            raise HTTPException(status_code=400, detail="Provide a valid .xls file")

        project.form_xls = contents
        db.commit()

    project.xform_title = category
    db.commit()

    # Update odk forms
    await project_crud.update_project_form(
        db, project_id, file_ext[1:] if upload else "xls", upload  # Form
    )

    return JSONResponse(status_code=200, content={"success": True})


@router.get("/download_template/")
async def download_template(category: str, db: Session = Depends(database.get_db)):
    """Download an XLSForm template to fill out."""
    xlsform_path = f"{xlsforms_path}/{category}.xls"
    if os.path.exists(xlsform_path):
        return FileResponse(xlsform_path, filename="form.xls")
    else:
        raise HTTPException(status_code=404, detail="Form not found")


@router.get("/{project_id}/download")
async def download_project_boundary(
    project_id: int,
    db: Session = Depends(database.get_db),
):
    """Downloads the boundary of a project as a GeoJSON file.

    Args:
        project_id (int): The id of the project.
        db (Session): The database session, provided automatically.

    Returns:
        Response: The HTTP response object containing the downloaded file.
    """
    out = await project_crud.get_project_geometry(db, project_id)
    headers = {
        "Content-Disposition": "attachment; filename=project_outline.geojson",
        "Content-Type": "application/media",
    }

    return Response(content=out, headers=headers)


@router.get("/{project_id}/download_tasks")
async def download_task_boundaries(
    project_id: int,
    db: Session = Depends(database.get_db),
):
    """Downloads the boundary of the tasks for a project as a GeoJSON file.

    Args:
        project_id (int): The id of the project.
        db (Session): The database session, provided automatically.

    Returns:
        Response: The HTTP response object containing the downloaded file.
    """
    out = await project_crud.get_task_geometry(db, project_id)

    headers = {
        "Content-Disposition": "attachment; filename=project_outline.geojson",
        "Content-Type": "application/media",
    }

    return Response(content=out, headers=headers)


@router.get("/features/download/")
async def download_features(project_id: int, db: Session = Depends(database.get_db)):
    """Downloads the features of a project as a GeoJSON file.

    Args:
        project_id (int): The id of the project.
        db (Session): The database session, provided automatically.

    Returns:
        Response: The HTTP response object containing the downloaded file.
    """
    out = await project_crud.get_project_features_geojson(db, project_id)

    headers = {
        "Content-Disposition": "attachment; filename=project_features.geojson",
        "Content-Type": "application/media",
    }

    return Response(content=json.dumps(out), headers=headers)


@router.get("/tiles/{project_id}")
async def generate_project_tiles(
    background_tasks: BackgroundTasks,
    project_id: int,
    source: str = Query(
        ..., description="Select a source for tiles", enum=TILES_SOURCE
    ),
    format: str = Query(
        "mbtiles", description="Select an output format", enum=TILES_FORMATS
    ),
    tms: str = Query(
        None,
        description="Provide a custom TMS URL, optional",
    ),
    db: Session = Depends(database.get_db),
):
    """Returns basemap tiles for a project.

    Args:
        background_tasks (BackgroundTasks): FastAPI bg tasks, provided automatically.
        project_id (int): ID of project to create tiles for.
        source (str): Tile source ("esri", "bing", "topo", "google", "oam").
        format (str, optional): Default "mbtiles". Other options: "pmtiles", "sqlite3".
        tms (str, optional): Default None. Custom TMS provider URL.
        db (Session): The database session, provided automatically.

    Returns:
        str: Success message that tile generation started.
    """
    # Create task in db and return uuid
    log.debug(
        "Creating generate_project_tiles background task "
        f"for project ID: {project_id}"
    )
    background_task_id = await project_crud.insert_background_task_into_database(
        db, project_id=project_id
    )

    background_tasks.add_task(
        project_crud.get_project_tiles,
        db,
        project_id,
        background_task_id,
        source,
        format,
        tms,
    )

    return {"Message": "Tile generation started"}


@router.get("/tiles_list/{project_id}/")
async def tiles_list(project_id: int, db: Session = Depends(database.get_db)):
    """Returns the list of tiles for a project.

    Parameters:
        project_id: int
        db (Session): The database session, provided automatically.

    Returns:
        Response: List of generated tiles for a project.
    """
    return await project_crud.get_mbtiles_list(db, project_id)


@router.get("/download_tiles/")
async def download_tiles(tile_id: int, db: Session = Depends(database.get_db)):
    """Download the basemap tile archive for a project."""
    log.debug("Getting tile archive path from DB")
    tiles_path = (
        db.query(db_models.DbTilesPath)
        .filter(db_models.DbTilesPath.id == str(tile_id))
        .first()
    )
    log.info(f"User requested download for tiles: {tiles_path.path}")

    project_id = tiles_path.project_id
    project_name = await project_crud.get_project(db, project_id).project_name_prefix
    filename = Path(tiles_path.path).name.replace(
        f"{project_id}_", f"{project_name.replace(' ', '_')}_"
    )
    log.debug(f"Sending tile archive to user: {filename}")

    return FileResponse(
        tiles_path.path,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/boundary_in_osm/{project_id}/")
async def download_task_boundary_osm(
    project_id: int,
    db: Session = Depends(database.get_db),
):
    """Downloads the boundary of a task as a OSM file.

    Args:
        project_id (int): The id of the project.
        db (Session): The database session, provided automatically.

    Returns:
        Response: The HTTP response object containing the downloaded file.
    """
    out = await project_crud.get_task_geometry(db, project_id)
    file_path = f"/tmp/{project_id}_task_boundary.geojson"

    # Write the response content to the file
    with open(file_path, "w") as f:
        f.write(out)
    result = await project_crud.convert_geojson_to_osm(file_path)

    with open(result, "r") as f:
        content = f.read()

    response = Response(content=content, media_type="application/xml")
    return response


@router.get("/centroid/")
async def project_centroid(
    project_id: int = None,
    db: Session = Depends(database.get_db),
):
    """Get a centroid of each projects.

    Parameters:
        project_id (int): The ID of the project.
        db (Session): The database session, provided automatically.

    Returns:
        list[tuple[int, str]]: A list of tuples containing the task ID and
            the centroid as a string.
    """
    query = text(
        f"""SELECT id,
            ARRAY_AGG(ARRAY[ST_X(ST_Centroid(outline)),
            ST_Y(ST_Centroid(outline))]) AS centroid
            FROM projects
            WHERE {f"id={project_id}" if project_id else "1=1"}
            GROUP BY id;"""
    )

    result = db.execute(query)
    result_dict_list = [{"id": row[0], "centroid": row[1]} for row in result.fetchall()]
    return result_dict_list


@router.get("/task-status/{uuid}", response_model=project_schemas.BackgroundTaskStatus)
async def get_task_status(
    task_uuid: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(database.get_db),
):
    """Get the background task status by passing the task UUID."""
    # Get the backgrund task status
    task_status, task_message = await project_crud.get_background_task_status(
        task_uuid, db
    )
    return project_schemas.BackgroundTaskStatus(
        status=task_status.name,
        message=task_message or None,
        # progress=some_func_to_get_progress,
    )


@router.get("/templates/")
async def get_template_file(
    file_type: str = Query(
        ..., enum=["data_extracts", "form"], description="Choose file type"
    )
):
    """Get template file.

    Args: file_type: Type of template file.

    returns: Requested file as a FileResponse.
    """
    file_type_paths = {
        "data_extracts": f"{data_path}/template/template.geojson",
        "form": f"{data_path}/template/template.xls",
    }
    file_path = file_type_paths.get(file_type)
    filename = file_path.split("/")[-1]
    return FileResponse(
        file_path, media_type="application/octet-stream", filename=filename
    )


@router.get(
    "/project_dashboard/{project_id}", response_model=project_schemas.ProjectDashboard
)
async def project_dashboard(
    project_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(database.get_db),
):
    """Get the project dashboard details.

    Args:
        project_id (int): The ID of the project.
        background_tasks (BackgroundTasks): FastAPI bg tasks, provided automatically.
        db (Session): The database session.

    Returns:
        ProjectDashboard: The project dashboard details.
    """
    data = await project_crud.get_dashboard_detail(project_id, db)
    background_task_id = await project_crud.insert_background_task_into_database(
        db, "sync_submission", project_id
    )

    background_tasks.add_task(
        submission_crud.update_submission_in_s3, db, project_id, background_task_id
    )
    return data


@router.get("/contributors/{project_id}")
async def get_contributors(project_id: int, db: Session = Depends(database.get_db)):
    """Get contributors of a project.

    Args:
        project_id (int): ID of project.
        db (Session): The database session.

    Returns:
        list[project_schemas.ProjectUser]: List of project users.
    """
    project_users = await project_crud.get_project_users(db, project_id)
    return project_users
