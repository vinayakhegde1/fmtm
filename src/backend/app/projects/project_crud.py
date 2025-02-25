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
"""Logic for FMTM project routes."""

import io
import json
import os
import time
import uuid
from asyncio import gather
from concurrent.futures import ThreadPoolExecutor, wait
from importlib.resources import files as pkg_files
from io import BytesIO
from typing import List, Optional, Union

import geoalchemy2
import geojson
import requests
import segno
import shapely.wkb as wkblib
import sozipfile.sozipfile as zipfile
import sqlalchemy
from asgiref.sync import async_to_sync
from fastapi import File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fmtm_splitter.splitter import split_by_sql, split_by_square
from geoalchemy2.shape import from_shape, to_shape
from geojson.feature import Feature, FeatureCollection
from loguru import logger as log
from osm_fieldwork.basemapper import create_basemap_file
from osm_fieldwork.data_models import data_models_path
from osm_fieldwork.filter_data import FilterData
from osm_fieldwork.json2osm import json2osm
from osm_fieldwork.OdkCentral import OdkAppUser
from osm_fieldwork.xlsforms import xlsforms_path
from osm_rawdata.postgres import PostgresClient
from shapely import to_geojson, wkt
from shapely.geometry import (
    Polygon,
    shape,
)
from shapely.ops import unary_union
from sqlalchemy import and_, column, func, inspect, select, table, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.central import central_crud
from app.config import settings
from app.db import db_models
from app.db.database import get_db
from app.db.postgis_utils import geojson_to_flatgeobuf, geometry_to_geojson, timestamp
from app.organization import organization_crud
from app.projects import project_schemas
from app.s3 import add_obj_to_bucket, get_obj_from_bucket
from app.tasks import tasks_crud
from app.users import user_crud

QR_CODES_DIR = "QR_codes/"
TASK_GEOJSON_DIR = "geojson/"
TILESDIR = "/opt/tiles"


async def get_projects(
    db: Session,
    user_id: int,
    skip: int = 0,
    limit: int = 100,
    hashtags: List[str] = None,
    search: str = None,
):
    """Get all projects."""
    filters = []
    if user_id:
        filters.append(db_models.DbProject.author_id == user_id)

    if hashtags:
        filters.append(db_models.DbProject.hashtags.op("&&")(hashtags))

    if search:
        filters.append(db_models.DbProject.project_name_prefix.ilike(f"%{search}%"))

    if len(filters) > 0:
        db_projects = (
            db.query(db_models.DbProject)
            .filter(and_(*filters))
            .order_by(db_models.DbProject.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        project_count = db.query(db_models.DbProject).filter(and_(*filters)).count()

    else:
        db_projects = (
            db.query(db_models.DbProject)
            .order_by(db_models.DbProject.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        project_count = db.query(db_models.DbProject).count()
    return project_count, await convert_to_app_projects(db_projects)


async def get_project_summaries(
    db: Session,
    user_id: int,
    skip: int = 0,
    limit: int = 100,
    hashtags: str = None,
    search: str = None,
):
    """Get project summary details for main page."""
    project_count, db_projects = await get_projects(
        db, user_id, skip, limit, hashtags, search
    )
    return project_count, await convert_to_project_summaries(db_projects)


async def get_project(db: Session, project_id: int):
    """Get a single project."""
    db_project = (
        db.query(db_models.DbProject)
        .filter(db_models.DbProject.id == project_id)
        .first()
    )
    return db_project


async def get_project_by_id(db: Session, project_id: int):
    """Get a single project by id."""
    db_project = (
        db.query(db_models.DbProject)
        .filter(db_models.DbProject.id == project_id)
        .first()
    )
    return await convert_to_app_project(db_project)


async def get_project_info_by_id(db: Session, project_id: int):
    """Get the project info only by id."""
    db_project_info = (
        db.query(db_models.DbProjectInfo)
        .filter(db_models.DbProjectInfo.project_id == project_id)
        .order_by(db_models.DbProjectInfo.project_id)
        .first()
    )
    return await convert_to_app_project_info(db_project_info)


async def delete_one_project(db: Session, db_project: db_models.DbProject) -> None:
    """Delete a project by id."""
    try:
        project_id = db_project.id
        db.delete(db_project)
        db.commit()
        log.info(f"Deleted project with ID: {project_id}")
    except Exception as e:
        log.exception(e)
        raise HTTPException(e) from e


async def partial_update_project_info(
    db: Session, project_metadata: project_schemas.ProjectUpdate, project_id
):
    """Partial project update for PATCH."""
    # Get the project from db
    db_project = await get_project_by_id(db, project_id)

    # Raise an exception if project is not found.
    if not db_project:
        raise HTTPException(
            status_code=428, detail=f"Project with id {project_id} does not exist"
        ) from None

    # Get project info
    db_project_info = await get_project_info_by_id(db, project_id)

    # Update project informations
    if project_metadata.name:
        db_project.project_name_prefix = project_metadata.name
        db_project_info.name = project_metadata.name
    if project_metadata.description:
        db_project_info.description = project_metadata.description
    if project_metadata.short_description:
        db_project_info.short_description = project_metadata.short_description

    db.commit()
    db.refresh(db_project)

    return await convert_to_app_project(db_project)


async def update_project_info(
    db: Session, project_metadata: project_schemas.ProjectUpload, project_id
):
    """Full project update for PUT."""
    user = project_metadata.author
    project_info = project_metadata.project_info

    # verify data coming in
    if not user:
        raise HTTPException("No user passed in")
    if not project_info:
        raise HTTPException("No project info passed in")

    # get db user
    db_user = await user_crud.get_user(db, user.id)
    if not db_user:
        raise HTTPException(
            status_code=400, detail=f"User {user.username} does not exist"
        )

    # verify project exists in db
    db_project = await get_project_by_id(db, project_id)
    if not db_project:
        raise HTTPException(
            status_code=428, detail=f"Project with id {project_id} does not exist"
        )

    # Project meta informations
    project_info = project_metadata.project_info

    # Update author of the project
    db_project.author = db_user
    db_project.project_name_prefix = project_info.name

    # get project info
    db_project_info = await get_project_info_by_id(db, project_id)

    # Update projects meta informations (name, descriptions)
    db_project_info.name = project_info.name
    db_project_info.short_description = project_info.short_description
    db_project_info.description = project_info.description

    db.commit()
    db.refresh(db_project)

    return await convert_to_app_project(db_project)


async def create_project_with_project_info(
    db: Session, project_metadata: project_schemas.ProjectUpload, odk_project_id: int
):
    """Create a new project, including all associated info."""
    # FIXME the ProjectUpload model should be converted to the db model directly
    # FIXME we don't need to extract each variable and pass manually
    project_user = project_metadata.author
    project_info = project_metadata.project_info
    xform_title = project_metadata.xform_title
    odk_credentials = project_metadata.odk_central
    hashtags = project_metadata.hashtags
    organisation_id = project_metadata.organisation_id
    task_split_type = project_metadata.task_split_type
    task_split_dimension = project_metadata.task_split_dimension
    task_num_buildings = project_metadata.task_num_buildings
    data_extract_type = project_metadata.task_num_buildings

    # verify data coming in
    if not project_user:
        raise HTTPException("User details are missing")
    if not project_info:
        raise HTTPException("Project info is missing")
    if not odk_project_id:
        raise HTTPException("ODK Central project id is missing")

    log.debug(
        "Creating project in FMTM database with vars: "
        f"project_user: {project_user} | "
        f"project_info: {project_info} | "
        f"xform_title: {xform_title} | "
        f"hashtags: {hashtags}| "
        f"organisation_id: {organisation_id}"
    )

    # Check / set credentials
    if odk_credentials:
        url = odk_credentials.odk_central_url
        user = odk_credentials.odk_central_user
        pw = odk_credentials.odk_central_password

    else:
        log.debug("ODKCentral connection variables not set in function")
        log.debug("Attempting extraction from environment variables")
        url = settings.ODK_CENTRAL_URL
        user = settings.ODK_CENTRAL_USER
        pw = settings.ODK_CENTRAL_PASSWD

    # get db user
    # TODO: get this from logged in user / request instead,
    # return 403 (forbidden) if not authorized
    db_user = await user_crud.get_user(db, project_user.id)
    if not db_user:
        raise HTTPException(
            status_code=400, detail=f"User {project_user.username} does not exist"
        )

    hashtags = (
        list(
            map(
                lambda hashtag: hashtag if hashtag.startswith("#") else f"#{hashtag}",
                hashtags,
            )
        )
        if hashtags
        else None
    )
    # create new project
    db_project = db_models.DbProject(
        author=db_user,
        odkid=odk_project_id,
        project_name_prefix=project_info.name,
        xform_title=xform_title,
        odk_central_url=url,
        odk_central_user=user,
        odk_central_password=pw,
        hashtags=hashtags,
        organisation_id=organisation_id,
        task_split_type=task_split_type,
        task_split_dimension=task_split_dimension,
        task_num_buildings=task_num_buildings,
        data_extract_type=data_extract_type,
        # country=[project_metadata.country],
        # location_str=f"{project_metadata.city}, {project_metadata.country}",
    )
    db.add(db_project)

    # add project info (project id needed to create project info)
    db_project_info = db_models.DbProjectInfo(
        project=db_project,
        name=project_info.name,
        short_description=project_info.short_description,
        description=project_info.description,
    )
    db.add(db_project_info)

    db.commit()
    db.refresh(db_project)

    return await convert_to_app_project(db_project)


async def upload_xlsform(
    db: Session,
    xlsform: str,
    name: str,
    category: str,
):
    """Upload a custom XLSForm from the user."""
    try:
        forms = table(
            "xlsforms",
            column("title"),
            column("xls"),
            column("xml"),
            column("id"),
            column("category"),
        )
        ins = insert(forms).values(title=name, xls=xlsform, category=category)
        sql = ins.on_conflict_do_update(
            constraint="xlsforms_title_key",
            set_=dict(title=name, xls=xlsform, category=category),
        )
        db.execute(sql)
        db.commit()
        return True
    except Exception as e:
        log.exception(e)
        raise HTTPException(status=400, detail={"message": str(e)}) from e


async def update_multi_polygon_project_boundary(
    db: Session,
    project_id: int,
    boundary: str,
):
    """Update the boundary for a project & update tasks.

    TODO requires refactoring, as it has too large of
    a scope. It should update a project boundary only, then manage
    tasks in another function.

    This function receives the project_id and boundary as a parameter
    and creates a task for each polygon in the database.
    This function also creates a project outline from the multiple
    polygons received.
    """
    try:
        if isinstance(boundary, str):
            boundary = json.loads(boundary)

        # verify project exists in db
        db_project = await get_project_by_id(db, project_id)
        if not db_project:
            log.error(f"Project {project_id} doesn't exist!")
            return False

        # Update the boundary polyon on the database.
        if boundary["type"] == "Feature":
            polygons = [boundary]
        else:
            polygons = boundary["features"]
        log.debug(f"Processing {len(polygons)} task geometries")
        for polygon in polygons:
            # If the polygon is a MultiPolygon, convert it to a Polygon
            if polygon["geometry"]["type"] == "MultiPolygon":
                log.debug("Converting MultiPolygon to Polygon")
                polygon["geometry"]["type"] = "Polygon"
                polygon["geometry"]["coordinates"] = polygon["geometry"]["coordinates"][
                    0
                ]

            # def remove_z_dimension(coord):
            #     """Helper to remove z dimension.

            #     To be used in lambda, to remove z dimension from
            #     each coordinate in the feature's geometry.
            #     """
            #     return coord.pop() if len(coord) == 3 else None

            # # Apply the lambda function to each coordinate in its geometry
            # list(map(remove_z_dimension, polygon["geometry"]["coordinates"][0]))

            db_task = db_models.DbTask(
                project_id=project_id,
                outline=wkblib.dumps(shape(polygon["geometry"]), hex=True),
                project_task_index=1,
            )
            db.add(db_task)
            db.commit()

            # Id is passed in the task_name too
            db_task.project_task_name = str(db_task.id)
            log.debug(
                "Created database task | "
                f"Project ID {project_id} | "
                f"Task ID {db_task.project_task_name}"
            )
            db.commit()

        # Generate project outline from tasks
        query = text(
            f"""SELECT ST_AsText(ST_ConvexHull(ST_Collect(outline)))
                        FROM tasks
                        WHERE project_id={project_id};"""
        )

        log.debug("Generating project outline from tasks")
        result = db.execute(query)
        data = result.fetchone()

        await update_project_location_info(db_project, data[0])

        db.commit()
        db.refresh(db_project)
        log.debug("COMPLETE: creating project boundary, based on task boundaries")

        return True
    except Exception as e:
        log.exception(e)
        raise HTTPException(e) from e


async def preview_split_by_square(boundary: str, meters: int):
    """Preview split by square for a project boundary.

    Use a lambda function to remove the "z" dimension from each
    coordinate in the feature's geometry.
    """

    def remove_z_dimension(coord):
        """Remove z dimension from geojson."""
        return coord.pop() if len(coord) == 3 else None

    """ Check if the boundary is a Feature or a FeatureCollection """
    if boundary["type"] == "Feature":
        features = [boundary]
    elif boundary["type"] == "FeatureCollection":
        features = boundary["features"]
    elif boundary["type"] == "Polygon":
        features = [
            {
                "type": "Feature",
                "properties": {},
                "geometry": boundary,
            }
        ]
    else:
        raise HTTPException(
            status_code=400, detail=f"Invalid GeoJSON type: {boundary['type']}"
        )

    # Apply the lambda function to each coordinate in its geometry
    # to remove the z-dimension - if it exists
    multi_polygons = []
    for feature in features:
        list(map(remove_z_dimension, feature["geometry"]["coordinates"][0]))
        if feature["geometry"]["type"] == "MultiPolygon":
            multi_polygons.append(Polygon(feature["geometry"]["coordinates"][0][0]))

    # Merge multiple geometries into single polygon
    if multi_polygons:
        boundary = multi_polygons[0]
        for geom in multi_polygons[1:]:
            boundary = boundary.union(geom)

    return await run_in_threadpool(
        lambda: split_by_square(
            boundary,
            meters=meters,
        )
    )


async def get_data_extract_from_osm_rawdata(
    aoi: UploadFile,
    category: str,
):
    """Get data extract using OSM RawData module.

    Filters by a specific category.
    """
    try:
        # read entire file
        aoi_content = await aoi.read()
        boundary = json.loads(aoi_content)

        # Validatiing Coordinate Reference System
        check_crs(boundary)

        # Get pre-configured filter for category
        config_path = f"{data_models_path}/{category}.yaml"

        if boundary["type"] == "FeatureCollection":
            # Convert each feature into a Shapely geometry
            geometries = [
                shape(feature["geometry"]) for feature in boundary["features"]
            ]
            updated_geometry = unary_union(geometries)
        else:
            updated_geometry = shape(boundary["geometry"])

        # Convert the merged MultiPolygon to a single Polygon using convex hull
        merged_polygon = updated_geometry.convex_hull

        # Convert the merged polygon back to a GeoJSON-like dictionary
        boundary = {
            "type": "Feature",
            "geometry": to_geojson(merged_polygon),
            "properties": {},
        }

        # # OSM Extracts using raw data api
        pg = PostgresClient("underpass", config_path)
        data_extract = pg.execQuery(boundary)
        return data_extract
    except Exception as e:
        log.error(e)
        raise HTTPException(status_code=400, detail=str(e)) from e


async def get_data_extract_url(
    db: Session,
    aoi: Union[FeatureCollection, Feature, dict],
    project_id: Optional[int] = None,
) -> str:
    """Request an extract from raw-data-api and extract the file contents.

    - The query is posted to raw-data-api and job initiated for fetching the extract.
    - The status of the job is polled every few seconds, until 'SUCCESS' is returned.
    - The resulting flatgeobuf file is streamed in the frontend.

    Returns:
        str: the URL for the flatgeobuf data extract.
    """
    if project_id:
        db_project = await get_project_by_id(db, project_id)
        if not db_project:
            log.error(f"Project {project_id} doesn't exist!")
            return False

        # TODO update db field data_extract_type --> data_extract_url
        fgb_url = db_project.data_extract_type

        # If extract already exists, return url to it
        if fgb_url:
            return fgb_url

    # FIXME replace below with get_data_extract_from_osm_rawdata

    # Data extract does not exist, continue to create
    # Filters for osm extracts
    query = {
        "filters": {
            "tags": {
                "all_geometry": {
                    "join_or": {"building": [], "highway": [], "waterway": []}
                }
            }
        }
    }

    if geom_type := aoi.get("type") == "FeatureCollection":
        # Convert each feature into a Shapely geometry
        geometries = [
            shape(feature.get("geometry")) for feature in aoi.get("features", [])
        ]
        merged_geom = unary_union(geometries)
    elif geom_type == aoi.get("type") == "Feature":
        merged_geom = shape(aoi.get("geometry"))
    else:
        merged_geom = shape(aoi)
    # Convert the merged geoms to a single Polygon GeoJSON using convex hull
    query["geometry"] = json.loads(to_geojson(merged_geom.convex_hull))

    # Filename to generate
    # query["fileName"] = f"fmtm-project-{project_id}-extract"
    query["fileName"] = "fmtm-extract"
    # Output to flatgeobuf format
    query["outputType"] = "fgb"
    # Generate without zipping
    query["bind_zip"] = False
    # Optional authentication
    # headers["access-token"] = settings.OSM_SVC_ACCOUNT_TOKEN

    log.debug(f"Query for raw data api: {query}")
    base_url = settings.UNDERPASS_API_URL
    query_url = f"{base_url}/snapshot/"
    headers = {"accept": "application/json", "Content-Type": "application/json"}

    # Send the request to raw data api
    try:
        result = requests.post(query_url, data=json.dumps(query), headers=headers)
        result.raise_for_status()
    except requests.exceptions.HTTPError:
        error_dict = result.json()
        error_dict["status_code"] = result.status_code
        log.error(f"Failed to get extract from raw data api: {error_dict}")
        return error_dict

    task_id = result.json()["task_id"]

    # Check status of task (PENDING, or SUCCESS)
    task_url = f"{base_url}/tasks/status/{task_id}"
    while True:
        result = requests.get(task_url, headers=headers)
        if result.json()["status"] == "PENDING":
            # Wait 2 seconds before polling again
            time.sleep(2)
        elif result.json()["status"] == "SUCCESS":
            break

    fgb_url = result.json()["result"]["download_url"]
    return fgb_url


async def split_geojson_into_tasks(
    db: Session,
    project_geojson: Union[dict, FeatureCollection],
    extract_geojson: Union[dict, FeatureCollection],
    no_of_buildings: int,
):
    """Splits a project into tasks.

    Args:
        db (Session): A database session.
        project_geojson (Union[dict, FeatureCollection]): A GeoJSON of the project
            boundary.
        extract_geojson (Union[dict, FeatureCollection]): A GeoJSON of the project
            boundary osm data extract (features).
        no_of_buildings (int): The number of buildings to include in each task.

    Returns:
        Any: A GeoJSON object containing the tasks for the specified project.
    """
    log.debug("STARTED task splitting using provided boundary and data extract")
    features = await run_in_threadpool(
        lambda: split_by_sql(
            project_geojson,
            db,
            num_buildings=no_of_buildings,
            osm_extract=extract_geojson,
        )
    )
    log.debug("COMPLETE task splitting")
    return features


async def update_project_boundary(
    db: Session, project_id: int, boundary: str, meters: int
):
    """Update the boundary for a project and update tasks."""
    # verify project exists in db
    db_project = await get_project_by_id(db, project_id)
    if not db_project:
        log.error(f"Project {project_id} doesn't exist!")
        return False

    # Use a lambda function to remove the "z" dimension from each
    # coordinate in the feature's geometry
    def remove_z_dimension(coord):
        """Remove the z dimension from a geojson."""
        return coord.pop() if len(coord) == 3 else None

    """ Check if the boundary is a Feature or a FeatureCollection """
    if boundary["type"] == "Feature":
        features = [boundary]
    elif boundary["type"] == "FeatureCollection":
        features = boundary["features"]
    elif boundary["type"] == "Polygon":
        features = [
            {
                "type": "Feature",
                "properties": {},
                "geometry": boundary,
            }
        ]
    else:
        # Raise an exception
        raise HTTPException(
            status_code=400, detail=f"Invalid GeoJSON type: {boundary['type']}"
        )

    """ Apply the lambda function to each coordinate in its geometry """
    multi_polygons = []
    for feature in features:
        list(map(remove_z_dimension, feature["geometry"]["coordinates"][0]))
        if feature["geometry"]["type"] == "MultiPolygon":
            multi_polygons.append(Polygon(feature["geometry"]["coordinates"][0][0]))

    """Update the boundary polygon on the database."""
    if multi_polygons:
        outline = multi_polygons[0]
        for geom in multi_polygons[1:]:
            outline = outline.union(geom)
    else:
        outline = shape(features[0]["geometry"])

    await update_project_location_info(db_project, outline.wkt)

    db.commit()
    db.refresh(db_project)
    log.debug("Finished updating project boundary")

    log.debug("Splitting tasks")
    tasks = split_by_square(
        boundary,
        meters=meters,
    )
    for poly in tasks["features"]:
        log.debug(poly)
        task_id = str(poly.get("properties", {}).get("id") or poly.get("id"))
        db_task = db_models.DbTask(
            project_id=project_id,
            project_task_name=task_id,
            outline=wkblib.dumps(shape(poly["geometry"]), hex=True),
            # qr_code=db_qr,
            # qr_code_id=db_qr.id,
            # project_task_index=feature["properties"]["fid"],
            project_task_index=1,
            # geometry_geojson=geojson.dumps(task_geojson),
            # initial_feature_count=len(task_geojson["features"]),
        )
        db.add(db_task)
        db.commit()

        # FIXME: write to tasks table
    return True


async def update_project_with_zip(
    db: Session,
    project_id: int,
    project_name_prefix: str,
    task_type_prefix: str,
    uploaded_zip: UploadFile,
):
    """Update a project from a zip file.

    TODO ensure that logged in user is user who created this project,
    return 403 (forbidden) if not authorized.
    """
    # ensure file upload is zip
    if uploaded_zip.content_type not in [
        "application/zip",
        "application/zip-compressed",
        "application/x-zip-compressed",
    ]:
        raise HTTPException(
            status_code=415,
            detail=f"File must be a zip. Uploaded file was {uploaded_zip.content_type}",
        )

    with zipfile.ZipFile(io.BytesIO(uploaded_zip.file.read()), "r") as zip:
        # verify valid zip file
        bad_file = zip.testzip()
        if bad_file:
            raise HTTPException(
                status_code=400, detail=f"Zip contained a bad file: {bad_file}"
            )

        # verify zip includes top level files & directories
        listed_files = zip.namelist()

        if QR_CODES_DIR not in listed_files:
            raise HTTPException(
                status_code=400,
                detail=f"Zip must contain directory named {QR_CODES_DIR}",
            )

        if TASK_GEOJSON_DIR not in listed_files:
            raise HTTPException(
                status_code=400,
                detail=f"Zip must contain directory named {TASK_GEOJSON_DIR}",
            )

        outline_filename = f"{project_name_prefix}.geojson"
        if outline_filename not in listed_files:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Zip must contain file named '{outline_filename}' "
                    "that contains a FeatureCollection outlining the project"
                ),
            )

        task_outlines_filename = f"{project_name_prefix}_polygons.geojson"
        if task_outlines_filename not in listed_files:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Zip must contain file named '{task_outlines_filename}' "
                    "that contains a FeatureCollection where each Feature "
                    "outlines a task"
                ),
            )

        # verify project exists in db
        db_project = await get_project_by_id(db, project_id)
        if not db_project:
            raise HTTPException(
                status_code=428, detail=f"Project with id {project_id} does not exist"
            )

        # add prefixes
        db_project.project_name_prefix = project_name_prefix
        db_project.task_type_prefix = task_type_prefix

        # generate outline from file and add to project
        outline_shape = await get_outline_from_geojson_file_in_zip(
            zip, outline_filename, f"Could not generate Shape from {outline_filename}"
        )
        await update_project_location_info(db_project, outline_shape.wkt)

        # get all task outlines from file
        project_tasks_feature_collection = await get_json_from_zip(
            zip,
            task_outlines_filename,
            f"Could not generate FeatureCollection from {task_outlines_filename}",
        )

        # generate task for each feature
        try:
            task_count = 0
            db_project.total_tasks = len(project_tasks_feature_collection["features"])
            for feature in project_tasks_feature_collection["features"]:
                task_name = feature["properties"]["task"]

                # generate and save qr code in db
                qr_filename = (
                    f"{project_name_prefix}_{task_type_prefix}__{task_name}.png"
                )
                db_qr = await get_dbqrcode_from_file(
                    zip,
                    QR_CODES_DIR + qr_filename,
                    (
                        f"QRCode for task {task_name} does not exist. "
                        f"File should be in {qr_filename}"
                    ),
                )
                db.add(db_qr)

                # save outline
                task_outline_shape = await get_shape_from_json_str(
                    feature,
                    f"Could not create task outline for {task_name} using {feature}",
                )

                # extract task geojson
                task_geojson_filename = (
                    f"{project_name_prefix}_{task_type_prefix}__{task_name}.geojson"
                )
                task_geojson = await get_json_from_zip(
                    zip,
                    TASK_GEOJSON_DIR + task_geojson_filename,
                    f"Geojson for task {task_name} does not exist",
                )

                # generate qr code id first
                db.flush()
                # save task in db
                task = db_models.DbTask(
                    project_id=project_id,
                    project_task_index=feature["properties"]["fid"],
                    project_task_name=task_name,
                    qr_code=db_qr,
                    qr_code_id=db_qr.id,
                    outline=task_outline_shape.wkt,
                    # geometry_geojson=json.dumps(task_geojson),
                    initial_feature_count=len(task_geojson["features"]),
                )
                db.add(task)

                # for error messages
                task_count = task_count + 1
            db_project.last_updated = timestamp()

            db.commit()
            # should now include outline, geometry and tasks
            db.refresh(db_project)

            return db_project

        # Exception was raised by app logic and has an error message,
        # just pass it along
        except HTTPException as e:
            log.error(e)
            raise e from None

        # Unexpected exception
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"{task_count} tasks were created before the "
                    f"following error was thrown: {e}, on feature: {feature}"
                ),
            ) from e


# ---------------------------
# ---- SUPPORT FUNCTIONS ----
# ---------------------------


async def read_xlsforms(
    db: Session,
    directory: str,
):
    """Read the list of XLSForms from the disk."""
    xlsforms = list()
    package_name = "osm_fieldwork"
    package_files = pkg_files(package_name)
    for xls in os.listdir(directory):
        if xls.endswith(".xls") or xls.endswith(".xlsx"):
            file_name = xls.split(".")[0]
            yaml_file_name = f"data_models/{file_name}.yaml"
            if package_files.joinpath(yaml_file_name).is_file():
                xlsforms.append(xls)
            else:
                continue
    log.info(xls)
    inspect(db_models.DbXForm)
    forms = table(
        "xlsforms", column("title"), column("xls"), column("xml"), column("id")
    )
    # x = Table('xlsforms', MetaData())
    # x.primary_key.columns.values()

    for xlsform in xlsforms:
        infile = f"{directory}/{xlsform}"
        if os.path.getsize(infile) <= 0:
            log.warning(f"{infile} is empty!")
            continue
        xls = open(infile, "rb")
        name = xlsform.split(".")[0]
        data = xls.read()
        xls.close()
        # log.info(xlsform)
        ins = insert(forms).values(title=name, xls=data)
        sql = ins.on_conflict_do_update(
            constraint="xlsforms_title_key", set_=dict(title=name, xls=data)
        )
        db.execute(sql)
        db.commit()

    return xlsforms


async def get_odk_id_for_project(db: Session, project_id: int):
    """Get the odk project id for the fmtm project id."""
    project = table(
        "projects",
        column("odkid"),
    )

    where = f"id={project_id}"
    sql = select(project).where(text(where))
    log.info(str(sql))
    result = db.execute(sql)

    # There should only be one match
    if result.rowcount != 1:
        log.warning(str(sql))
        return False
    project_info = result.first()
    return project_info.odkid


async def upload_custom_data_extract(
    db: Session,
    project_id: int,
    geojson_str: str,
) -> str:
    """Uploads custom data extracts to S3.

    Args:
        db (Session): SQLAlchemy database session.
        project_id (int): The ID of the project.
        geojson_str (str): The custom data extracts contents.

    Returns:
        str: URL to fgb file in S3.
    """
    project = await get_project(db, project_id)
    log.debug(f"Uploading custom data extract for project: {project}")

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    log.debug("Parsing geojson file")
    geojson_parsed = geojson.loads(geojson_str)
    if isinstance(geojson_parsed, FeatureCollection):
        log.debug("Already in FeatureCollection format, skipping reparse")
        featcol = geojson_parsed
    elif isinstance(geojson_parsed, Feature):
        log.debug("Converting Feature to FeatureCollection")
        featcol = FeatureCollection(geojson_parsed)
    else:
        log.debug("Converting geometry to FeatureCollection")
        featcol = FeatureCollection[Feature(geometry=geojson_parsed)]

    # Validating Coordinate Reference System
    check_crs(featcol)

    # FIXME use osm-fieldwork filter/clean data
    # cleaned = FilterData()
    # models = xlsforms_path.replace("xlsforms", "data_models")
    # xlsfile = f"{category}.xls"  # FIXME: for custom form
    # file = f"{xlsforms_path}/{xlsfile}"
    # if os.path.exists(file):
    #     title, extract = cleaned.parse(file)
    # elif os.path.exists(f"{file}x"):
    #     title, extract = cleaned.parse(f"{file}x")
    # # Remove anything in the data extract not in the choices sheet.
    # cleaned_data = cleaned.cleanData(features_data)
    feature_type = featcol.get("features", [])[-1].get("geometry", {}).get("type")
    if feature_type not in ["Polygon", "Polyline"]:
        msg = (
            "Extract does not contain valid geometry types, from 'Polygon' "
            "and 'Polyline'"
        )
        log.error(msg)
        raise HTTPException(status_code=404, detail=msg)
    features_filtered = [
        feature
        for feature in featcol.get("features", [])
        if feature.get("geometry", {}).get("type", "") == feature_type
    ]
    featcol_filtered = FeatureCollection(features_filtered)

    log.debug(
        "Generating fgb object from geojson with "
        f"{len(featcol_filtered.get('features', []))} features"
    )
    fgb_obj = BytesIO(await geojson_to_flatgeobuf(db, featcol_filtered))
    s3_fgb_path = f"/{project.organisation_id}/{project_id}/custom_extract.fgb"

    log.debug(f"Uploading fgb to S3 path: {s3_fgb_path}")
    add_obj_to_bucket(
        settings.S3_BUCKET_NAME,
        fgb_obj,
        s3_fgb_path,
        content_type="application/octet-stream",
    )

    # Add url to database
    s3_fgb_url = f"{settings.S3_DOWNLOAD_ROOT}/{settings.S3_BUCKET_NAME}{s3_fgb_path}"
    log.debug(f"Commiting extract S3 path to database: {s3_fgb_url}")
    project.data_extract_type = s3_fgb_url
    db.commit()

    return s3_fgb_url


def flatten_dict(d, parent_key="", sep="_"):
    """Recursively flattens a nested dictionary into a single-level dictionary.

    Args:
        d (dict): The input dictionary.
        parent_key (str): The parent key (used for recursion).
        sep (str): The separator character to use in flattened keys.

    Returns:
        dict: The flattened dictionary.
    """
    items = {}
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.update(flatten_dict(v, new_key, sep=sep))
        else:
            items[new_key] = v
    return items


# NOTE defined as non-async to run in separate thread
def generate_task_files(
    db: Session,
    project_id: int,
    task_id: int,
    xlsform: str,
    form_type: str,
    odk_credentials: project_schemas.ODKCentral,
):
    """Generate all files for a task."""
    project_log = log.bind(task="create_project", project_id=project_id)

    project_log.info(f"Generating files for task {task_id}")

    get_project_sync = async_to_sync(get_project)
    project = get_project_sync(db, project_id)

    odk_id = project.odkid
    project_name = project.project_name_prefix
    category = project.xform_title
    name = f"{project_name}_{category}_{task_id}"

    # Create an app user for the task
    project_log.info(f"Creating odkcentral app user for task {task_id}")
    appuser = central_crud.create_appuser(odk_id, name, odk_credentials)

    # If app user could not be created, raise an exception.
    if not appuser:
        project_log.error("Couldn't create appuser for project")
        return False

    # prefix should be sent instead of name
    project_log.info(f"Creating qr code for task {task_id}")
    create_qr_sync = async_to_sync(create_qrcode)
    qr_code = create_qr_sync(
        db,
        odk_id,
        appuser.json()["token"],
        project_name,
        odk_credentials.odk_central_url,
    )

    get_task_sync = async_to_sync(tasks_crud.get_task)
    task = get_task_sync(db, task_id)
    task.qr_code_id = qr_code["qr_code_id"]
    db.commit()
    db.refresh(task)

    # This file will store xml contents of an xls form.
    xform = f"/tmp/{name}.xml"
    extracts = f"/tmp/{name}.geojson"  # This file will store osm extracts

    # xform_id_format
    xform_id = f"{name}".split("_")[2]

    # Get the features for this task.
    # Postgis query to filter task inside this task outline and of this project
    # Update those features and set task_id
    query = text(
        f"""UPDATE features
                SET task_id={task_id}
                WHERE id IN (
                    SELECT id
                    FROM features
                    WHERE project_id={project_id}
                    AND ST_IsValid(geometry)
                    AND ST_IsValid('{task.outline}'::Geometry)
                    AND ST_Contains('{task.outline}'::Geometry, ST_Centroid(geometry))
                )"""
    )

    result = db.execute(query)

    # Get the geojson of those features for this task.
    query = text(
        f"""SELECT jsonb_build_object(
                'type', 'FeatureCollection',
                'features', jsonb_agg(feature)
                )
                FROM (
                SELECT jsonb_build_object(
                    'type', 'Feature',
                    'id', id,
                    'geometry', ST_AsGeoJSON(geometry)::jsonb,
                    'properties', properties
                ) AS feature
                FROM features
                WHERE project_id={project_id} and task_id={task_id}
                ) features;"""
    )

    result = db.execute(query)

    features = result.fetchone()[0]

    upload_media = False if features["features"] is None else True

    # Update outfile containing osm extracts with the new geojson contents
    # containing title in the properties.
    with open(extracts, "w") as jsonfile:
        jsonfile.truncate(0)  # clear the contents of the file
        geojson.dump(features, jsonfile)

    project_log.info(
        f"Generating xform for task: {task_id} "
        f"using xform: {xform} | form_type: {form_type}"
    )
    outfile = central_crud.generate_updated_xform(xlsform, xform, form_type)

    # Create an odk xform
    project_log.info(f"Uploading media in {task_id}")
    result = central_crud.create_odk_xform(
        odk_id, task_id, outfile, odk_credentials, False, upload_media
    )
    # result = central_crud.create_odk_xform(odk_id, task_id, outfile, odk_credentials)

    project_log.info(f"Updating role for app user in task {task_id}")
    # Update the user role for the created xform.
    try:
        # Pass odk credentials
        if odk_credentials:
            url = odk_credentials.odk_central_url
            user = odk_credentials.odk_central_user
            pw = odk_credentials.odk_central_password

        else:
            log.debug("ODKCentral connection variables not set in function")
            log.debug("Attempting extraction from environment variables")
            url = settings.ODK_CENTRAL_URL
            user = settings.ODK_CENTRAL_USER
            pw = settings.ODK_CENTRAL_PASSWD

        odk_app = OdkAppUser(url, user, pw)

        odk_app.updateRole(
            projectId=odk_id, xform=xform_id, actorId=appuser.json()["id"]
        )
    except Exception as e:
        log.exception(e)

    project.extract_completed_count += 1
    db.commit()
    db.refresh(project)

    return True


# NOTE defined as non-async to run in separate thread
def generate_appuser_files(
    db: Session,
    project_id: int,
    extract_polygon: bool,
    custom_xls_form: str,
    extracts_contents: str,
    category: str,
    form_type: str,
    background_task_id: Optional[uuid.UUID] = None,
):
    """Generate the files for a project.

    QR code, new XForm, and the OSM data extract.

    Parameters:
        - db: the database session
        - project_id: Project ID
        - extract_polygon: boolean to determine if we should extract the polygon
        - custom_xls_form: the xls file to upload if we have a custom form
        - extracts_contents: the custom data extract
        - category: the category of the project
        - form_type: weather the form is xls, xlsx or xml
        - background_task_id: the task_id of the background task running this function.
    """
    try:
        project_log = log.bind(task="create_project", project_id=project_id)

        project_log.info(f"Starting generate_appuser_files for project {project_id}")

        # Get the project table contents.
        project = table(
            "projects",
            column("project_name_prefix"),
            column("xform_title"),
            column("id"),
            column("odk_central_url"),
            column("odk_central_user"),
            column("odk_central_password"),
            column("outline"),
        )

        where = f"id={project_id}"
        sql = select(
            project.c.project_name_prefix,
            project.c.xform_title,
            project.c.id,
            project.c.odk_central_url,
            project.c.odk_central_user,
            project.c.odk_central_password,
            geoalchemy2.functions.ST_AsGeoJSON(project.c.outline).label("outline"),
        ).where(text(where))
        result = db.execute(sql)

        # There should only be one match
        if result.rowcount != 1:
            log.warning(str(sql))
            if result.rowcount < 1:
                raise HTTPException(status_code=400, detail="Project not found")
            else:
                raise HTTPException(status_code=400, detail="Multiple projects found")

        one = result.first()

        if one:
            # Get odk credentials from project.
            odk_credentials = {
                "odk_central_url": one.odk_central_url,
                "odk_central_user": one.odk_central_user,
                "odk_central_password": one.odk_central_password,
            }

            odk_credentials = project_schemas.ODKCentral(**odk_credentials)

            xform_title = one.xform_title if one.xform_title else None

            category = xform_title
            if custom_xls_form:
                xlsform = f"/tmp/{category}.{form_type}"
                contents = custom_xls_form
                with open(xlsform, "wb") as f:
                    f.write(contents)
            else:
                xlsform = f"{xlsforms_path}/{xform_title}.xls"

            # Data Extracts
            if extracts_contents is not None:
                project_log.info("Uploading data extracts")
                upload_extract_sync = async_to_sync(upload_custom_data_extract)
                upload_extract_sync(db, project_id, extracts_contents)

            else:
                project = (
                    db.query(db_models.DbProject)
                    .filter(db_models.DbProject.id == project_id)
                    .first()
                )
                config_file_contents = project.form_config_file

                project_log.info("Extracting Data from OSM")

                config_path = "/tmp/config.yaml"
                if config_file_contents:
                    with open(config_path, "w", encoding="utf-8") as config_file_handle:
                        config_file_handle.write(config_file_contents.decode("utf-8"))
                else:
                    config_path = f"{data_models_path}/{category}.yaml"

                # # OSM Extracts for whole project
                pg = PostgresClient("underpass", config_path)
                outline = json.loads(one.outline)
                boundary = {"type": "Feature", "properties": {}, "geometry": outline}
                data_extract = pg.execQuery(boundary)
                filter = FilterData(xlsform)

                updated_data_extract = {"type": "FeatureCollection", "features": []}
                filtered_data_extract = (
                    filter.cleanData(data_extract)
                    if data_extract
                    else updated_data_extract
                )

                # Collect feature mappings for bulk insert
                feature_mappings = []

                for feature in filtered_data_extract["features"]:
                    # If the osm extracts contents do not have a title,
                    # provide an empty text for that.
                    feature["properties"]["title"] = ""

                    feature_shape = shape(feature["geometry"])

                    # If the centroid of the Polygon is not inside the outline,
                    # skip the feature.
                    if extract_polygon and (
                        not shape(outline).contains(shape(feature_shape.centroid))
                    ):
                        continue

                    wkb_element = from_shape(feature_shape, srid=4326)
                    feature_mapping = {
                        "project_id": project_id,
                        "category_title": category,
                        "geometry": wkb_element,
                        "properties": feature["properties"],
                    }
                    updated_data_extract["features"].append(feature)
                    feature_mappings.append(feature_mapping)
                # Bulk insert the osm extracts into the db.
                db.bulk_insert_mappings(db_models.DbFeatures, feature_mappings)

            # Generating QR Code, XForm and uploading OSM Extracts to the form.
            # Creating app users and updating the role of that user.
            get_task_id_list_sync = async_to_sync(tasks_crud.get_task_id_list)
            task_list = get_task_id_list_sync(db, project_id)

            # Run with expensive task via threadpool
            def wrap_generate_task_files(task):
                """Func to wrap and return errors from thread.

                Also passes it's own database session for thread safety.
                If we pass a single db session to multiple threads,
                there may be inconsistencies or errors.
                """
                try:
                    generate_task_files(
                        next(get_db()),
                        project_id,
                        task,
                        xlsform,
                        form_type,
                        odk_credentials,
                    )
                except Exception as e:
                    log.exception(str(e))

            # Use a ThreadPoolExecutor to run the synchronous code in threads
            with ThreadPoolExecutor() as executor:
                # Submit tasks to the thread pool
                futures = [
                    executor.submit(wrap_generate_task_files, task)
                    for task in task_list
                ]
                # Wait for all tasks to complete
                wait(futures)

        if background_task_id:
            # Update background task status to COMPLETED
            update_bg_task_sync = async_to_sync(
                update_background_task_status_in_database
            )
            update_bg_task_sync(db, background_task_id, 4)  # 4 is COMPLETED

    except Exception as e:
        log.warning(str(e))

        if background_task_id:
            # Update background task status to FAILED
            update_bg_task_sync = async_to_sync(
                update_background_task_status_in_database
            )
            update_bg_task_sync(db, background_task_id, 2, str(e))  # 2 is FAILED
        else:
            # Raise original error if not running in background
            raise e


async def create_qrcode(
    db: Session,
    odk_id: int,
    token: str,
    project_name: str,
    odk_central_url: str = None,
):
    """Create a QR code for a task."""
    # Make QR code for an app_user.
    log.debug(f"Generating base64 encoded QR settings for token: {token}")
    qrcode_data = await central_crud.create_qrcode(
        odk_id, token, project_name, odk_central_url
    )

    log.debug("Generating QR code from base64 settings")
    qrcode = segno.make(qrcode_data, micro=False)

    log.debug("Saving to buffer and decoding")
    buffer = io.BytesIO()
    qrcode.save(buffer, kind="png", scale=5)
    qrcode_binary = buffer.getvalue()

    log.debug(f"Writing QR code to database for token {token}")
    qrdb = db_models.DbQrCode(image=qrcode_binary)
    db.add(qrdb)
    db.commit()
    codes = table("qr_code", column("id"))
    sql = select(sqlalchemy.func.count(codes.c.id))
    result = db.execute(sql)
    rows = result.fetchone()[0]
    return {"data": qrcode, "id": rows + 1, "qr_code_id": qrdb.id}


async def get_project_geometry(db: Session, project_id: int):
    """Retrieves the geometry of a project.

    Args:
        db (Session): The database session.
        project_id (int): The ID of the project.

    Returns:
        str: A geojson of the project outline.
    """
    projects = table("projects", column("outline"), column("id"))
    where = f"projects.id={project_id}"
    sql = select(geoalchemy2.functions.ST_AsGeoJSON(projects.c.outline)).where(
        text(where)
    )
    result = db.execute(sql)
    # There should only be one match
    if result.rowcount != 1:
        log.warning(str(sql))
        return False
    row = eval(result.first()[0])
    return json.dumps(row)


async def get_task_geometry(db: Session, project_id: int):
    """Retrieves the geometry of tasks associated with a project.

    Args:
        db (Session): The database session.
        project_id (int): The ID of the project.

    Returns:
        str: A geojson of the task boundaries
    """
    db_tasks = await tasks_crud.get_tasks(db, project_id, None)
    features = []
    for task in db_tasks:
        geom = to_shape(task.outline)
        # Convert the shapely geometry object to GeoJSON
        geometry = geom.__geo_interface__
        properties = {
            "task_id": task.id,
        }
        feature = {"type": "Feature", "geometry": geometry, "properties": properties}
        features.append(feature)

    feature_collection = {"type": "FeatureCollection", "features": features}
    return json.dumps(feature_collection)


async def get_project_features_geojson(db: Session, project_id: int):
    """Get a geojson of all features for a task."""
    db_features = (
        db.query(db_models.DbFeatures)
        .filter(db_models.DbFeatures.project_id == project_id)
        .all()
    )

    query = text(
        f"""SELECT jsonb_build_object(
                'type', 'FeatureCollection',
                'features', jsonb_agg(feature)
                )
                FROM (
                SELECT jsonb_build_object(
                    'type', 'Feature',
                    'id', id,
                    'geometry', ST_AsGeoJSON(geometry)::jsonb,
                    'properties', properties
                ) AS feature
                FROM features
                WHERE project_id={project_id}
                ) features;
            """
    )

    result = db.execute(query)
    features = result.fetchone()[0]

    # Create mapping feat_id:task_id
    task_feature_mapping = {feat.id: feat.task_id for feat in db_features}

    for feature in features["features"]:
        if (feat_id := feature["id"]) in task_feature_mapping:
            feature["properties"]["task_id"] = task_feature_mapping[feat_id]

    return features


async def get_json_from_zip(zip, filename: str, error_detail: str):
    """Extract json file from zip."""
    try:
        with zip.open(filename) as file:
            data = file.read()
            return json.loads(data)
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=400, detail=f"{error_detail} ----- Error: {e}"
        ) from e


async def get_outline_from_geojson_file_in_zip(
    zip, filename: str, error_detail: str, feature_index: int = 0
):
    """Parse geojson outline within a zip."""
    try:
        with zip.open(filename) as file:
            data = file.read()
            json_dump = json.loads(data)
            check_crs(json_dump)  # Validatiing Coordinate Reference System
            feature_collection = FeatureCollection(json_dump)
            feature = feature_collection["features"][feature_index]
            geom = feature["geometry"]
            shape_from_geom = shape(geom)
            return shape_from_geom
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=400,
            detail=f"{error_detail} ----- Error: {e} ----",
        ) from e


async def get_shape_from_json_str(feature: str, error_detail: str):
    """Parse geojson outline within a zip to shapely geom."""
    try:
        geom = feature["geometry"]
        return shape(geom)
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=400,
            detail=f"{error_detail} ----- Error: {e} ---- Json: {feature}",
        ) from e


async def get_dbqrcode_from_file(zip, qr_filename: str, error_detail: str):
    """Get qr code from database during import."""
    try:
        with zip.open(qr_filename) as qr_file:
            binary_qrcode = qr_file.read()
            if binary_qrcode:
                return db_models.DbQrCode(
                    filename=qr_filename,
                    image=binary_qrcode,
                )
            else:
                raise HTTPException(
                    status_code=400, detail=f"{qr_filename} is an empty file"
                ) from None
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=400, detail=f"{error_detail} ----- Error: {e}"
        ) from e


# --------------------
# ---- CONVERTERS ----
# --------------------

# TODO: write tests for these


async def convert_to_app_project(db_project: db_models.DbProject):
    """Legacy function to convert db models --> Pydantic.

    TODO refactor to use Pydantic model methods instead.
    """
    if not db_project:
        log.debug("convert_to_app_project called, but no project provided")
        return None

    log.debug("Converting db project to app project")
    app_project: project_schemas.Project = db_project

    if db_project.outline:
        log.debug("Converting project outline to geojson")
        app_project.outline_geojson = geometry_to_geojson(
            db_project.outline, {"id": db_project.id}, db_project.id
        )

    app_project.project_tasks = db_project.tasks

    return app_project


async def convert_to_app_project_info(db_project_info: db_models.DbProjectInfo):
    """Legacy function to convert db models --> Pydantic.

    TODO refactor to use Pydantic model methods instead.
    """
    if db_project_info:
        app_project_info: project_schemas.ProjectInfo = db_project_info
        return app_project_info
    else:
        return None


async def convert_to_app_projects(
    db_projects: List[db_models.DbProject],
) -> List[project_schemas.ProjectOut]:
    """Legacy function to convert db models --> Pydantic.

    TODO refactor to use Pydantic model methods instead.
    """
    if db_projects and len(db_projects) > 0:

        async def convert_project(project):
            return await convert_to_app_project(project)

        app_projects = await gather(
            *[convert_project(project) for project in db_projects]
        )
        return [project for project in app_projects if project is not None]
    else:
        return []


async def convert_to_project_summary(db_project: db_models.DbProject):
    """Legacy function to convert db models --> Pydantic.

    TODO refactor to use Pydantic model methods instead.
    """
    if db_project:
        summary: project_schemas.ProjectSummary = db_project

        if db_project.project_info:
            summary.location_str = db_project.location_str
            summary.title = db_project.project_info.name
            summary.description = db_project.project_info.short_description

        summary.num_contributors = (
            db_project.tasks_mapped + db_project.tasks_validated
        )  # TODO: get real number of contributors
        summary.organisation_logo = (
            db_project.organisation.logo if db_project.organisation else None
        )

        return summary
    else:
        return None


async def convert_to_project_summaries(
    db_projects: List[db_models.DbProject],
) -> List[project_schemas.ProjectSummary]:
    """Legacy function to convert db models --> Pydantic.

    TODO refactor to use Pydantic model methods instead.
    """
    if db_projects and len(db_projects) > 0:

        async def convert_summary(project):
            return await convert_to_project_summary(project)

        project_summaries = await gather(
            *[convert_summary(project) for project in db_projects]
        )
        return [summary for summary in project_summaries if summary is not None]
    else:
        return []


async def convert_to_project_feature(db_project_feature: db_models.DbFeatures):
    """Legacy function to convert db models --> Pydantic.

    TODO refactor to use Pydantic model methods instead.
    """
    if db_project_feature:
        app_project_feature: project_schemas.Feature = db_project_feature

        if db_project_feature.geometry:
            app_project_feature.geometry = geometry_to_geojson(
                db_project_feature.geometry,
                db_project_feature.properties,
                db_project_feature.id,
            )

        return app_project_feature
    else:
        return None


async def convert_to_project_features(
    db_project_features: List[db_models.DbFeatures],
) -> List[project_schemas.Feature]:
    """Legacy function to convert db models --> Pydantic.

    TODO refactor to use Pydantic model methods instead.
    """
    if db_project_features and len(db_project_features) > 0:

        async def convert_feature(project_feature):
            return await convert_to_project_feature(project_feature)

        app_project_features = await gather(
            *[convert_feature(feature) for feature in db_project_features]
        )
        return [feature for feature in app_project_features if feature is not None]
    else:
        return []


async def get_project_features(db: Session, project_id: int, task_id: int = None):
    """Get features from database for a project."""
    if task_id:
        features = (
            db.query(db_models.DbFeatures)
            .filter(db_models.DbFeatures.project_id == project_id)
            .filter(db_models.DbFeatures.task_id == task_id)
            .all()
        )
    else:
        features = (
            db.query(db_models.DbFeatures)
            .filter(db_models.DbFeatures.project_id == project_id)
            .all()
        )
    return await convert_to_project_features(features)


async def get_background_task_status(task_id: uuid.UUID, db: Session):
    """Get the status of a background task."""
    task = (
        db.query(db_models.BackgroundTasks)
        .filter(db_models.BackgroundTasks.id == str(task_id))
        .first()
    )
    if not task:
        log.warning(f"No background task with found with UUID: {task_id}")
        raise HTTPException(status_code=404, detail="Task not found")
    return task.status, task.message


async def insert_background_task_into_database(
    db: Session, name: str = None, project_id: str = None
) -> uuid.uuid4:
    """Inserts a new task into the database.

    Args:
        db (Session): database session
        name (str): name of the task.
        project_id (str): associated project id

    Returns:
        task_id (uuid.uuid4): The background task uuid for tracking.
    """
    task_id = uuid.uuid4()

    task = db_models.BackgroundTasks(
        id=str(task_id), name=name, status=1, project_id=project_id
    )  # 1 = running

    db.add(task)
    db.commit()
    db.refresh(task)

    return task_id


async def update_background_task_status_in_database(
    db: Session, task_id: uuid.UUID, status: int, message: str = None
) -> None:
    """Updates the status of a task in the database.

    Args:
        db (Session): database session.
        task_id (uuid.UUID): uuid of the task.
        status (int): status of the task.
        message (str): optional message to add to the db task.

    Returns:
        None
    """
    db.query(db_models.BackgroundTasks).filter(
        db_models.BackgroundTasks.id == str(task_id)
    ).update(
        {
            db_models.BackgroundTasks.status: status,
            db_models.BackgroundTasks.message: message,
        }
    )
    db.commit()

    return True


async def update_project_form(
    db: Session, project_id: int, form_type: str, form: UploadFile = File(None)
):
    """Upload a new custom XLSForm for a project."""
    project = await get_project(db, project_id)
    category = project.xform_title
    project_title = project.project_name_prefix
    odk_id = project.odkid

    # ODK Credentials
    odk_credentials = project_schemas.ODKCentral(
        odk_central_url=project.odk_central_url,
        odk_central_user=project.odk_central_user,
        odk_central_password=project.odk_central_password,
    )

    if form:
        xlsform = f"/tmp/custom_form.{form_type}"
        contents = await form.read()
        with open(xlsform, "wb") as f:
            f.write(contents)
    else:
        xlsform = f"{xlsforms_path}/{category}.xls"

    db.query(db_models.DbFeatures).filter(
        db_models.DbFeatures.project_id == project_id
    ).delete()
    db.commit()

    # OSM Extracts for whole project
    pg = PostgresClient("underpass")
    outfile = (
        f"/tmp/{project_title}_{category}.geojson"  # This file will store osm extracts
    )

    extract_polygon = True if project.data_extract_type == "polygon" else False

    project = table("projects", column("outline"))

    # where = f"id={project_id}
    sql = select(
        geoalchemy2.functions.ST_AsGeoJSON(project.c.outline).label("outline"),
    ).where(text(f"id={project_id}"))
    result = db.execute(sql)
    project_outline = result.first()

    final_outline = json.loads(project_outline.outline)

    outline_geojson = pg.getFeatures(
        boundary=final_outline,
        filespec=outfile,
        polygon=extract_polygon,
        xlsfile=f"{category}.xls",
        category=category,
    )

    updated_outline_geojson = {"type": "FeatureCollection", "features": []}

    # Collect feature mappings for bulk insert
    feature_mappings = []

    for feature in outline_geojson["features"]:
        # If the osm extracts contents do not have a title,
        # provide an empty text for that.
        feature["properties"]["title"] = ""

        feature_shape = shape(feature["geometry"])

        # # If the centroid of the Polygon is not inside the outline,
        # skip the feature.
        # if extract_polygon and (
        #     not shape(outline_geojson).contains(
        #       shape(feature_shape.centroid
        #     ))
        # ):
        #     continue

        wkb_element = from_shape(feature_shape, srid=4326)
        feature_mapping = {
            "project_id": project_id,
            "category_title": category,
            "geometry": wkb_element,
            "properties": feature["properties"],
        }
        updated_outline_geojson["features"].append(feature)
        feature_mappings.append(feature_mapping)

        # Insert features into db
        db_feature = db_models.DbFeatures(
            project_id=project_id,
            category_title=category,
            geometry=wkb_element,
            properties=feature["properties"],
        )
        db.add(db_feature)
        db.commit()

    tasks_list = await tasks_crud.get_task_id_list(db, project_id)

    for task in tasks_list:
        task_obj = await tasks_crud.get_task(db, task)

        # Get the features for this task.
        # Postgis query to filter task inside this task outline and of this project
        # Update those features and set task_id
        query = text(
            f"""UPDATE features
                    SET task_id={task}
                    WHERE id in (
                    SELECT id
                    FROM features
                    WHERE project_id={project_id} and
                    ST_Intersects(geometry, '{task_obj.outline}'::Geometry)
            )"""
        )

        result = db.execute(query)

        # Get the geojson of those features for this task.
        query = text(
            f"""SELECT jsonb_build_object(
                    'type', 'FeatureCollection',
                    'features', jsonb_agg(feature)
                    )
                    FROM (
                    SELECT jsonb_build_object(
                        'type', 'Feature',
                        'id', id,
                        'geometry', ST_AsGeoJSON(geometry)::jsonb,
                        'properties', properties
                    ) AS feature
                    FROM features
                    WHERE project_id={project_id} and task_id={task}
                    ) features;"""
        )

        result = db.execute(query)
        features = result.fetchone()[0]
        # This file will store xml contents of an xls form.
        xform = f"/tmp/{project_title}_{category}_{task}.xml"
        # This file will store osm extracts
        extracts = f"/tmp/{project_title}_{category}_{task}.geojson"

        # Update outfile containing osm extracts with the new geojson contents
        #  containing title in the properties.
        with open(extracts, "w") as jsonfile:
            jsonfile.truncate(0)  # clear the contents of the file
            geojson.dump(features, jsonfile)

        outfile = central_crud.generate_updated_xform(xlsform, xform, form_type)

        # Create an odk xform
        result = central_crud.create_odk_xform(
            odk_id, task, xform, odk_credentials, True, True, False
        )

    return True


async def get_extracted_data_from_db(db: Session, project_id: int, outfile: str):
    """Get the geojson of those features for this project."""
    query = text(
        f"""SELECT jsonb_build_object(
                'type', 'FeatureCollection',
                'features', jsonb_agg(feature)
                )
                FROM (
                SELECT jsonb_build_object(
                    'type', 'Feature',
                    'id', id,
                    'geometry', ST_AsGeoJSON(geometry)::jsonb,
                    'properties', properties
                ) AS feature
                FROM features
                WHERE project_id={project_id}
                ) features;"""
    )

    result = db.execute(query)
    features = result.fetchone()[0]

    # Update outfile containing osm extracts with the new geojson contents
    # containing title in the properties.
    with open(outfile, "w") as jsonfile:
        jsonfile.truncate(0)
        geojson.dump(features, jsonfile)


# NOTE defined as non-async to run in separate thread
def get_project_tiles(
    db: Session,
    project_id: int,
    background_task_id: uuid.UUID,
    source: str,
    output_format: str = "mbtiles",
    tms: str = None,
):
    """Get the tiles for a project.

    Args:
        db (Session): SQLAlchemy db session.
        project_id (int): ID of project to create tiles for.
        background_task_id (uuid.UUID): UUID of background task to track.
        source (str): Tile source ("esri", "bing", "topo", "google", "oam").
        output_format (str, optional): Default "mbtiles".
            Other options: "pmtiles", "sqlite3".
        tms (str, optional): Default None. Custom TMS provider URL.
    """
    zooms = "12-19"
    tiles_path_id = uuid.uuid4()
    tiles_dir = f"{TILESDIR}/{tiles_path_id}"
    outfile = f"{tiles_dir}/{project_id}_{source}tiles.{output_format}"

    tile_path_instance = db_models.DbTilesPath(
        project_id=project_id,
        background_task_id=str(background_task_id),
        status=1,
        tile_source=source,
        path=outfile,
    )

    try:
        db.add(tile_path_instance)
        db.commit()

        # Project Outline
        log.debug(f"Getting bbox for project: {project_id}")
        query = text(
            f"""SELECT ST_XMin(ST_Envelope(outline)) AS min_lon,
                        ST_YMin(ST_Envelope(outline)) AS min_lat,
                        ST_XMax(ST_Envelope(outline)) AS max_lon,
                        ST_YMax(ST_Envelope(outline)) AS max_lat
                FROM projects
                WHERE id = {project_id};"""
        )

        result = db.execute(query)
        project_bbox = result.fetchone()
        log.debug(f"Extracted project bbox: {project_bbox}")

        if project_bbox:
            min_lon, min_lat, max_lon, max_lat = project_bbox
        else:
            log.error(f"Failed to get bbox from project: {project_id}")

        log.debug(
            "Creating basemap with params: "
            f"boundary={min_lon},{min_lat},{max_lon},{max_lat} | "
            f"outfile={outfile} | "
            f"zooms={zooms} | "
            f"outdir={tiles_dir} | "
            f"source={source} | "
            f"xy={False} | "
            f"tms={tms}"
        )
        create_basemap_file(
            boundary=f"{min_lon},{min_lat},{max_lon},{max_lat}",
            outfile=outfile,
            zooms=zooms,
            outdir=tiles_dir,
            source=source,
            xy=False,
            tms=tms,
        )
        log.info(f"Basemap created for project ID {project_id}: {outfile}")

        tile_path_instance.status = 4
        db.commit()

        # Update background task status to COMPLETED
        update_bg_task_sync = async_to_sync(update_background_task_status_in_database)
        update_bg_task_sync(db, background_task_id, 4)  # 4 is COMPLETED

        log.info(f"Tiles generation process completed for project id {project_id}")

    except Exception as e:
        log.exception(str(e))
        log.error(f"Tiles generation process failed for project id {project_id}")

        tile_path_instance.status = 2
        db.commit()

        # Update background task status to FAILED
        update_bg_task_sync = async_to_sync(update_background_task_status_in_database)
        update_bg_task_sync(db, background_task_id, 2, str(e))  # 2 is FAILED


async def get_mbtiles_list(db: Session, project_id: int):
    """List mbtiles in database for a project."""
    try:
        tiles_list = (
            db.query(
                db_models.DbTilesPath.id,
                db_models.DbTilesPath.project_id,
                db_models.DbTilesPath.status,
                db_models.DbTilesPath.tile_source,
            )
            .filter(db_models.DbTilesPath.project_id == str(project_id))
            .all()
        )

        processed_tiles_list = [
            {
                "id": x.id,
                "project_id": x.project_id,
                "status": x.status.name,
                "tile_source": x.tile_source,
            }
            for x in tiles_list
        ]

        return processed_tiles_list

    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=400, detail=str(e)) from e


async def convert_geojson_to_osm(geojson_file: str):
    """Convert a GeoJSON file to OSM format."""
    return json2osm(geojson_file)


async def get_address_from_lat_lon(latitude, longitude):
    """Get address using Nominatim, using lat,lon."""
    base_url = "https://nominatim.openstreetmap.org/reverse"

    params = {
        "format": "json",
        "lat": latitude,
        "lon": longitude,
        "zoom": 18,
    }
    headers = {"Accept-Language": "en"}  # Set the language to English

    response = requests.get(base_url, params=params, headers=headers)
    data = response.json()
    address = data["address"]["country"]

    if response.status_code == 200:
        if "city" in data["address"]:
            city = data["address"]["city"]
            address = f"{city}" + "," + address
        return address
    else:
        return "Address not found."


async def update_project_location_info(
    db_project: sqlalchemy.orm.declarative_base, project_boundary: str
):
    """Update project boundary, centroid, address.

    Args:
        db_project(sqlalchemy.orm.declarative_base): The project database record.
        project_boundary(str): WKT string geometry.
    """
    db_project.outline = project_boundary
    centroid = (wkt.loads(project_boundary)).centroid.wkt
    db_project.centroid = centroid
    geometry = wkt.loads(centroid)
    longitude, latitude = geometry.x, geometry.y
    address = await get_address_from_lat_lon(latitude, longitude)
    db_project.location_str = address if address is not None else ""


def check_crs(input_geojson: Union[dict, FeatureCollection]):
    """Validate CRS is valid for a geojson."""
    log.debug("validating coordinate reference system")

    def is_valid_crs(crs_name):
        valid_crs_list = [
            "urn:ogc:def:crs:OGC:1.3:CRS84",
            "urn:ogc:def:crs:EPSG::4326",
            "WGS 84",
        ]
        return crs_name in valid_crs_list

    def is_valid_coordinate(coord):
        if coord is None:
            return False
        return -180 <= coord[0] <= 180 and -90 <= coord[1] <= 90

    error_message = (
        "ERROR: Unsupported coordinate system, it is recommended to use a "
        "GeoJSON file in WGS84(EPSG 4326) standard."
    )
    if "crs" in input_geojson:
        crs = input_geojson.get("crs", {}).get("properties", {}).get("name")
        if not is_valid_crs(crs):
            log.error(error_message)
            raise HTTPException(status_code=400, detail=error_message)
        return

    if input_geojson_type := input_geojson.get("type") == "FeatureCollection":
        features = input_geojson.get("features", [])
        coordinates = (
            features[-1].get("geometry", {}).get("coordinates", []) if features else []
        )
    elif input_geojson_type := input_geojson.get("type") == "Feature":
        coordinates = input_geojson.get("geometry", {}).get("coordinates", [])

    geometry_type = (
        features[0].get("geometry", {}).get("type")
        if input_geojson_type == "FeatureCollection" and features
        else input_geojson.get("geometry", {}).get("type", "")
    )
    if geometry_type == "MultiPolygon":
        first_coordinate = coordinates[0][0] if coordinates and coordinates[0] else None
    elif geometry_type == "Point":
        first_coordinate = coordinates if coordinates else None

    elif geometry_type == "LineString":
        first_coordinate = coordinates[0] if coordinates else None

    else:
        first_coordinate = coordinates[0][0] if coordinates else None

    if not is_valid_coordinate(first_coordinate):
        log.error(error_message)
        raise HTTPException(status_code=400, detail=error_message)


async def get_tasks_count(db: Session, project_id: int):
    """Get number of tasks for a project."""
    db_task = (
        db.query(db_models.DbProject)
        .filter(db_models.DbProject.id == project_id)
        .first()
    )
    task_count = len(db_task.tasks)
    return task_count


async def get_pagination(page: int, count: int, results_per_page: int, total: int):
    """Pagination result for splash page."""
    total_pages = (count + results_per_page - 1) // results_per_page
    has_next = (page * results_per_page) < count  # noqa: N806
    has_prev = page > 1  # noqa: N806

    pagination = project_schemas.PaginationInfo(
        has_next=has_next,
        has_prev=has_prev,
        next_num=page + 1 if has_next else None,
        page=page,
        pages=total_pages,
        prev_num=page - 1 if has_prev else None,
        per_page=results_per_page,
        total=total,
    )

    return pagination


async def get_dashboard_detail(project_id: int, db: Session):
    """Get project details for project dashboard."""
    project = await get_project(db, project_id)
    db_organization = await organization_crud.get_organisation_by_id(
        db, project.organisation_id
    )

    s3_project_path = f"/{project.organisation_id}/{project_id}"
    s3_submission_path = f"/{s3_project_path}/submission.zip"
    s3_submission_meta_path = f"/{s3_project_path}/submissions.meta.json"

    try:
        submission = get_obj_from_bucket(settings.S3_BUCKET_NAME, s3_submission_path)
        with zipfile.ZipFile(submission, "r") as zip_ref:
            with zip_ref.open("submissions.json") as file_in_zip:
                content = file_in_zip.read()
        content = json.loads(content)
        project.total_submission = len(content)
        submission_meta = get_obj_from_bucket(
            settings.S3_BUCKET_NAME, s3_submission_meta_path
        )
        project.last_active = (json.loads(submission_meta.getvalue()))[
            "last_submission"
        ]
    except ValueError:
        project.total_submission = 0
        pass

    contributors = (
        db.query(db_models.DbTaskHistory.user_id)
        .filter(
            db_models.DbTaskHistory.project_id == project_id,
            db_models.DbTaskHistory.user_id.isnot(None),
        )
        .distinct()
        .count()
    )

    project.total_tasks = await tasks_crud.get_task_count_in_project(db, project_id)
    project.organization, project.organization_logo = (
        db_organization.name,
        db_organization.logo,
    )
    project.total_contributors = contributors

    return project


async def get_project_users(db: Session, project_id: int):
    """Get the users and their contributions for a project.

    Args:
        db (Session): The database session.
        project_id (int): The ID of the project.

    Returns:
        List[Dict[str, Union[str, int]]]: A list of dictionaries containing
            the username and the number of contributions made by each user
            for the specified project.
    """
    contributors = (
        db.query(db_models.DbTaskHistory)
        .filter(db_models.DbTaskHistory.project_id == project_id)
        .all()
    )
    unique_user_ids = {
        user.user_id for user in contributors if user.user_id is not None
    }
    response = []

    for user_id in unique_user_ids:
        contributions = count_user_contributions(db, user_id, project_id)
        db_user = await user_crud.get_user(db, user_id)
        response.append({"user": db_user.username, "contributions": contributions})

    response = sorted(response, key=lambda x: x["contributions"], reverse=True)
    return response


def count_user_contributions(db: Session, user_id: int, project_id: int) -> int:
    """Count contributions for a specific user.

    Args:
        db (Session): The database session.
        user_id (int): The ID of the user.
        project_id (int): The ID of the project.

    Returns:
        int: The number of contributions made by the user for the specified
            project.
    """
    contributions_count = (
        db.query(func.count(db_models.DbTaskHistory.user_id))
        .filter(
            db_models.DbTaskHistory.user_id == user_id,
            db_models.DbTaskHistory.project_id == project_id,
        )
        .scalar()
    )

    return contributions_count
