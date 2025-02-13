import pathlib
from typing import Optional
import time

import numpy as np
from femwell import mode_solver

import gdsfactory as gf
from gdsfactory.config import logger
from gdsfactory.pdk import _ACTIVE_PDK, get_layer_stack
from gdsfactory.simulation.get_modes_path import get_modes_path_femwell
from gdsfactory.technology import LayerStack
from gdsfactory.types import CrossSectionSpec, PathType

from skfem import (
    Basis,
    ElementTriN1,
    ElementTriN2,
    ElementTriP0,
    ElementTriP1,
    ElementTriP2,
    Mesh,
)


def load_mesh_basis(mesh_filename: PathType):
    mesh = Mesh.load(mesh_filename)
    basis = Basis(mesh, ElementTriN2() * ElementTriP2())
    basis0 = basis.with_element(ElementTriP0())
    return mesh, basis0


def compute_cross_section_modes(
    cross_section: CrossSectionSpec,
    layerstack: LayerStack,
    wavelength: float = 1.55,
    num_modes: int = 4,
    order: int = 1,
    radius: float = np.inf,
    mesh_filename: str = "mesh.msh",
    dirpath: Optional[PathType] = None,
    filepath: Optional[PathType] = None,
    overwrite: bool = False,
    with_cache: bool = True,
    wafer_padding: float = 2.0,
    **kwargs,
):
    """Calculate effective index of a straight cross-section.

    Args:
        cross_section: gdsfactory cross-section.
        layerstack: gdsfactory layerstack.
        wavelength: wavelength (um).
        num_modes: number of modes to return.
        order: order of the mesh elements. 1: linear, 2: quadratic.
        radius: bend radius of the cross-section.
        mesh_filename (str, path): where to save the .msh file. If with_cache, will be filepath.msh.
        dirpath: Optional directory to store modes.
        filepath: Optional path to store modes.
        overwrite: Overwrite mode filepath if it exists.
        with_cache: write modes to filepath cache.
        wafer_padding: padding beyond bbox to add to WAFER layers.

    Keyword Args:
        resolutions (Dict): Pairs {"layername": {"resolution": float, "distance": "float}}
            to roughly control mesh refinement within and away from entity, respectively.
        mesh_scaling_factor (float): factor multiply mesh geometry by.
        default_resolution_min (float): gmsh minimal edge length.
        default_resolution_max (float): gmsh maximal edge length.
        background_tag (str): name of the background layer to add (default: no background added).
        background_padding (Tuple): [xleft, ydown, xright, yup] distances to add to the components and to fill with background_tag.
        global_meshsize_array: np array [x,y,z,lc] to parametrize the mesh.
        global_meshsize_interpolant_func: interpolating function for global_meshsize_array.
        extra_shapes_dict: Optional[OrderedDict] = OrderedDict of {key: geo} with key a label and geo a shapely (Multi)Polygon or (Multi)LineString of extra shapes to override component.
        merge_by_material: boolean, if True will merge polygons from layers with the same layer.material. Physical keys will be material in this case.
    """
    sim_settings = dict(
        wavelength=wavelength,
        num_modes=num_modes,
        radius=radius,
        order=order,
        wafer_padding=wafer_padding,
        **kwargs,
    )
    filepath = filepath or get_modes_path_femwell(
        cross_section=cross_section,
        dirpath=dirpath,
        layerstack=layerstack,
        **sim_settings,
    )
    filepath = pathlib.Path(filepath)
    mesh_filename = filepath.with_suffix(".msh") if with_cache else mesh_filename

    if with_cache and filepath.exists():
        if overwrite:
            filepath.unlink()

        else:
            logger.info(f"Simulation loaded from {filepath!r}")

            modes_dict = dict(np.load(filepath))
            mesh, basis0 = load_mesh_basis(mesh_filename)

            if order == 1:
                element = ElementTriN1() * ElementTriP1()
            elif order == 2:
                element = ElementTriN2() * ElementTriP2()
            else:
                raise AssertionError("Only order 1 and 2 implemented by now.")

            basis = basis0.with_element(element)

            return modes_dict["lams"], basis, modes_dict["xs"]

    # Get meshable component from cross-section
    c = gf.components.straight(length=10, cross_section=cross_section)
    bounds = c.bbox
    dx = np.diff(bounds[:, 0])[0]

    # Mesh
    mesh = c.to_gmsh(
        type="uz",
        xsection_bounds=[
            [dx / 2, bounds[0, 1] - wafer_padding],
            [dx / 2, bounds[1, 1] + wafer_padding],
        ],
        layer_stack=layerstack,
        filename=mesh_filename,
        wafer_padding=wafer_padding,
        **kwargs,
    )

    # Assign materials to mesh elements
    mesh, basis0 = load_mesh_basis(mesh_filename)
    epsilon = basis0.zeros(dtype=complex)
    for layername, layer in layerstack.layers.items():
        if layername in mesh.subdomains.keys():
            epsilon[basis0.get_dofs(elements=layername)] = (
                _ACTIVE_PDK.materials_index[layer.material](wavelength) ** 2
            )
        if "background_tag" in kwargs:
            epsilon[basis0.get_dofs(elements=kwargs["background_tag"])] = (
                _ACTIVE_PDK.materials_index[kwargs["background_tag"]](wavelength) ** 2
            )

    # Mode solve
    lams, basis, xs = mode_solver.compute_modes(
        basis0,
        epsilon,
        wavelength=wavelength,
        mu_r=1,
        num_modes=num_modes,
        order=order,
        radius=radius,
        solver="slepc",
    )

    if with_cache:
        modes_dict = {"lams": lams, "xs": xs}
        np.savez_compressed(filepath, **modes_dict)
        logger.info(f"Write mode to {filepath!r}")
    return lams, basis, xs


if __name__ == "__main__":
    start = time.time()

    filtered_layerstack = LayerStack(
        layers={
            k: get_layer_stack().layers[k]
            for k in (
                "core",
                "clad",
                "slab90",
                "box",
            )
        }
    )

    filtered_layerstack.layers["core"].thickness = 0.2

    resolutions = {
        "core": {"resolution": 0.02, "distance": 2},
        "clad": {"resolution": 0.2, "distance": 1},
        "box": {"resolution": 0.2, "distance": 1},
        "slab90": {"resolution": 0.05, "distance": 1},
    }
    lams, basis, xs = compute_cross_section_modes(
        cross_section="strip",
        layerstack=filtered_layerstack,
        wavelength=1.55,
        num_modes=4,
        order=1,
        radius=np.inf,
        mesh_filename="mesh.msh",
        resolutions=resolutions,
        overwrite=True,
        with_cache=True,
    )

    mode_solver.plot_mode(
        basis=basis,
        mode=np.real(xs[0]),
        plot_vectors=False,
        colorbar=True,
        title="E",
        direction="y",
    )
    end = time.time()
    print(end - start)
