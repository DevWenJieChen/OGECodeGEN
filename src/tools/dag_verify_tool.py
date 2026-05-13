from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional, List

_WRAPPER = r"""
import sys
import json
import traceback

def emit_err(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()

try:
    code_path = sys.argv[1]
    with open(code_path, "r", encoding="utf-8") as f:
        src = f.read()

    compiled = compile(src, code_path, "exec")

    g = {"__name__": "__main__"}
    exec(compiled, g, g)

    sys.exit(0)

except SyntaxError as e:
    emit_err({
        "ok": False,
        "stage": "syntax",
        "error": {
            "type": "SyntaxError",
            "message": e.msg,
            "lineno": e.lineno,
            "offset": e.offset,
            "text": (e.text or "").strip(),
            "traceback": traceback.format_exc(),
        }
    })
    sys.exit(10)

except Exception as e:
    emit_err({
        "ok": False,
        "stage": "runtime",
        "error": {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
    })
    sys.exit(20)
"""


def _extract_payload(stdout: str) -> Dict[str, Optional[str]]:
    """
    Extract dag/spaceParams from stdout (non-greedy matching).
    Convention: when code executes successfully, stdout contains:
      dag=<<...>>
      spaceParams=<<...>>
    """
    dags: List[str] = []
    space_params = None

    if stdout:
        dags = re.findall(r"dag=<<(.+?)>>", stdout, flags=re.DOTALL) or []

    m = re.search(r"spaceParams=<<(.+?)>>", stdout, flags=re.DOTALL)
    if m:
        space_params = m.group(1)

    return {"dag": dags, "spaceParams": space_params}


def verify_code_to_dag(code: str, timeout_s: int = 30) -> Dict[str, Any]:
    """
    Input: code (Python code string)
    Output (unified dict):
      - Success:
        {
          "ok": True,
          "dag": "<dag_str or json>",
          "spaceParams": "<optional>",
          "stdout": "<raw stdout>",   # optional: useful for debugging
        }
      - Failure:
        {
          "ok": False,
          "stage": "input|syntax|runtime|timeout|unknown",
          "exit_code": <int or None>,
          "err_type": "...",
          "err_message": "...",
          "err_traceback": "...",
          "stderr": "<optional>",
          "stdout": "<optional>"
        }
    """
    code = (code or "").strip()
    if not code:
        return {
            "ok": False,
            "stage": "input",
            "exit_code": None,
            "err_type": "EmptyCode",
            "err_message": "code is empty",
        }

    try:
        with tempfile.TemporaryDirectory() as td:
            code_path = os.path.join(td, "generated_code.py")
            wrapper_path = os.path.join(td, "verify_wrapper.py")

            with open(code_path, "w", encoding="utf-8") as f:
                f.write(code)

            with open(wrapper_path, "w", encoding="utf-8") as f:
                f.write(_WRAPPER)

            proc = subprocess.run(
                [sys.executable, wrapper_path, code_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )

            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            print(stdout)

            if proc.returncode == 0:
                payload = _extract_payload(stdout)
                if not payload["dag"]:
                    # Execution succeeded but no dag=<<...>> was printed
                    return {
                        "ok": False,
                        "stage": "ok_but_no_dag",
                        "exit_code": 0,
                        "err_type": "MissingDagOutput",
                        "err_message": "code executed successfully but no 'dag=<<...>>' found in stdout",
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                return {
                    "ok": True,
                    "dag": payload["dag"],
                    "spaceParams": payload["spaceParams"],
                    "stdout": stdout,
                }

            # Non-zero exit: try to parse the last stdout line as wrapper JSON (structured error)
            wrapper_payload = None
            if stdout:
                last_line = stdout.splitlines()[-1]
                try:
                    wrapper_payload = json.loads(last_line)
                except json.JSONDecodeError:
                    wrapper_payload = None

            if isinstance(wrapper_payload, dict) and wrapper_payload.get("ok") is False:
                err = wrapper_payload.get("error") or {}
                return {
                    "ok": False,
                    "stage": wrapper_payload.get("stage", "unknown"),
                    "exit_code": proc.returncode,
                    "err_type": err.get("type"),
                    "err_message": err.get("message"),
                    "err_traceback": err.get("traceback"),
                    "stderr": stderr or None,
                    "stdout": stdout or None,
                }

            # The wrapper itself crashed or produced unexpected output
            return {
                "ok": False,
                "stage": "unknown",
                "exit_code": proc.returncode,
                "err_type": "UnknownVerifyFailure",
                "err_message": "wrapper output is not a valid structured error json",
                "stdout": stdout or None,
                "stderr": stderr or None,
            }

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "stage": "timeout",
            "exit_code": None,
            "err_type": "TimeoutExpired",
            "err_message": f"code verify timeout after {timeout_s}s",
        }

if __name__ == "__main__":
    code = "import oge\n#This example uses the geographical detector to calculate the explanatory power of terrain factors (elevation, aspect, and slope) on the spatial differentiation of vegetation growth in the study area.\n#The core idea of the geographical detector is based on the assumption that if an independent variable has an important influence on a dependent variable, their spatial distributions should be similar.\n#Therefore, its differentiation and factor detection module calculates the explanatory power q of X on the spatial differentiation of Y by comparing the total variance of dependent variable Y over the whole region with the sum of within-stratum variances after stratifying influencing factor X.\n#The study area in this example is a typical mountainous region in the eastern Tianshan Mountains of Xinjiang, where the elevation range is wide and vegetation growth can be observed across multiple elevation ranges.\n#The experimental extent is the overlapping area between Landsat 8 path 144 row 030 imagery and ASTGTM N43E084 data.\n#Initialize\noge.initialize()\nservice = oge.Service()\n\n#Read data\nlandsat8 = service.getCoverage(coverageID=\"LC08_L1TP_144030_20230831_20230906_02_T1\", productID=\"LC08_L1TP_C02_T1\")\nlandsat8_double = service.getProcess(\"Coverage.toDouble\").execute(landsat8)\nDEM = service.getCoverage(coverageID=\"ASTGTM_N43E084\", productID=\"ASTER_GDEM_DEM30\")\n\n# Calculate NDVI\nndvi = service.getProcess(\"Coverage.normalizedDifference\").execute(landsat8_double, [\"B5\", \"B4\"])\nndvi_mul = service.getProcess( \"Coverage.multiplyNum\").execute(ndvi, 100.0)\n\n# Clip the overlapping area of the two raster datasets\nndvi_mul_clip = service.getProcess( \"Coverage.rasterUnion\").execute(DEM,ndvi_mul)\ndem_clip = service.getProcess( \"Coverage.rasterUnion\").execute(ndvi_mul_clip,DEM)\n\n#Calculate slope and aspect\nslope = service.getProcess( \"Coverage.slope\").execute(dem_clip, 0.00001171,3 )\naspect = service.getProcess( \"Coverage.aspect\").execute(dem_clip, 1)\n\n#Set rules and reclassify DEM, slope, aspect, and NDVI\n#Define the abnormal value\nNaN_value = -0\n#Reclassify DEM\ndem_reclass = service.getProcess( \"Coverage.reclass\").execute(dem_clip,\n            [(500, 1500, 1), (1500, 2500, 2),(2500, 3000, 3), (3000, 3500, 4), (3500, 4500, 5)],NaN_value)\n#Reclassify slope\nslope_reclass = service.getProcess( \"Coverage.reclass\").execute(slope,\n            [(0, 5, 1), (5, 15, 2),(15, 25, 3), (25, 35, 4), (35, 45, 5), (45, 90, 6)],NaN_value)\n#Reclassify aspect\naspect_reclass = service.getProcess( \"Coverage.reclass\").execute(aspect,\n    [(0, 60, 1), (60, 120, 2), (120, 180, 3),(180, 240, 4), (240, 300, 5), (300, 360, 6)],NaN_value)\n#Reclassify NDVI\nndvi_reclass = service.getProcess( \"Coverage.reclass\").execute(ndvi_mul_clip,\n                [(-50, 0, 1), (0, 10, 2),(10, 20, 3),(20, 30, 4), (30, 40, 5), (40, 50, 6)],NaN_value)\n\n#Render the reclassification result\nvis_params = {}\nndvi_reclass.styles(vis_params).getMap(\"Reclassified NDVI data\")\n\n#Define the normal value range of the dependent variable\nndvi_norExtent = [-100,100]\ngeo_res_dem = service.getProcess( \"Coverage.geoDetector\").execute(ndvi_mul_clip,\n    \"dem\",dem_reclass,ndvi_norExtent[0],ndvi_norExtent[1],NaN_value\n)\ngeo_res_slope = service.getProcess( \"Coverage.geoDetector\").execute(ndvi_mul_clip,\n    \"slope\",slope_reclass,ndvi_norExtent[0],ndvi_norExtent[1],NaN_value\n)\ngeo_res_aspect = service.getProcess( \"Coverage.geoDetector\").execute(ndvi_mul_clip,\n    \"aspect\",aspect_reclass,ndvi_norExtent[0],ndvi_norExtent[1],NaN_value\n)\ngeo_res_ndvi = service.getProcess( \"Coverage.geoDetector\").execute(ndvi_mul_clip,\n    \"ndvi\",ndvi_reclass,ndvi_norExtent[0],ndvi_norExtent[1],NaN_value\n)\n\n#Print results to the console\ngeo_res_dem.log(\"Elevation factor calculation result\")\ngeo_res_slope.log(\"Slope factor calculation result\")\ngeo_res_aspect.log(\"Aspect factor calculation result\")\ngeo_res_ndvi.log(\"NDVI factor (control group) calculation result\")\n\noge.mapclient.centerMap(85.22, 43.16, 10)\n#The final result consists of two parts\n#One part is the console output showing each factor's explanatory power for the spatial differentiation of the dependent variable (NDVI)\n#The results show elevation, slope, aspect (terrain factors), and NDVI (used as the control group after reclassification),\n#with explanatory powers for NDVI spatial differentiation of 0.59, 0.07, 0.02, and 0.96, respectively\n#indicating that the terrain effect on vegetation in this study area is mainly related to elevation,\n#and elevation has a strong influence on the spatial distribution of vegetation growth\n#The other part is the reclassified NDVI data displayed on the map,\n#which reflects the spatial distribution relationship of vegetation growth\n\n"
    code1 = "import oge\noge.initialize()\nservice = oge.Service()\n\n# ==================== 1. Data reading ====================\n# Option A: use a single Landsat 8 scene (recommended, low cloud cover)\nls8 = service.getCoverage(\n    coverageID=\"LC08_L1GT_121040_20240222_20240229_02_T2\",\n    productID=\"LC08_C02_L1\"\n)\n\n# Option B: use CoverageCollection for a larger area if needed (comment out the current option)\n# cov_col = service.getCoverageCollection(\n#     productID=\"LC08_C02_L1\",\n#     bbox=[115.3381468601, 27.7975616225, 117.6849501783, 29.9208764208],\n#     datetime=[\"2024-02-01 00:00:00\", \"2024-03-01 00:00:00\"]\n# )\n# ls8 = service.getProcess(\"CoverageCollection.mosaic\").execute(cov_col)\n\n# Read DEM data for the corresponding area (assuming ASTER GDEM is used)\n# Note: in actual applications, ensure the DEM covers the Landsat image area\ndem = service.getCoverage(\n    coverageID=\"ASTGTM_N28E116\",  # example ID; adjust according to the actual area\n    productID=\"ASTER_GDEM_DEM30\"\n)\n\n# ==================== 2. Data preprocessing ====================\n# Convert Landsat 8 to float to ensure calculation precision\nls8_float = service.getProcess(\"Coverage.toFloat\").execute(ls8)\n\n# ==================== 3. Calculate the vegetation index (NDVI) ====================\n# Use bands B5 (near infrared) and B4 (red)\nndvi = service.getProcess(\"Coverage.NDVI\").execute(ls8_float, \"B4\", \"B5\")\n\n# ==================== 4. Terrain factor calculation ====================\n# Calculate slope (using terrSlope, neighborhood radius 1, z-factor=1)\nslope = service.getProcess(\"Coverage.terrSlope\").execute(dem, 1, 1.0)\n\n# Calculate aspect (using terrAspect, neighborhood radius 1, z-factor=1)\naspect = service.getProcess(\"Coverage.terrAspect\").execute(dem, 1, 1.0)\n\n# Elevation is the DEM itself; no additional calculation is needed\n\n# ==================== 5. Spatial alignment ====================\n# Extract the overlapping area between NDVI and DEM\nndvi_aligned = service.getProcess(\"Coverage.rasterUnion\").execute(ndvi, dem)\ndem_aligned = service.getProcess(\"Coverage.rasterUnion\").execute(dem, ndvi)\nslope_aligned = service.getProcess(\"Coverage.rasterUnion\").execute(slope, ndvi)\naspect_aligned = service.getProcess(\"Coverage.rasterUnion\").execute(aspect, ndvi)\n\n# ==================== 6. Reclassification (preparation for the geographical detector) ====================\n# NDVI reclassification (example: 5 classes)\nndvi_rules = [\n    (-1.0, 0.1, 1.0),\n    (0.1, 0.3, 2.0),\n    (0.3, 0.5, 3.0),\n    (0.5, 0.7, 4.0),\n    (0.7, 1.0, 5.0)\n]\nndvi_reclass = service.getProcess(\"Coverage.reclass\").execute(ndvi_aligned, ndvi_rules, -9999)\n\n# Elevation reclassification (example: 4 classes)\nelev_rules = [\n    (0.0, 200.0, 1.0),\n    (200.0, 500.0, 2.0),\n    (500.0, 1000.0, 3.0),\n    (1000.0, 9999.0, 4.0)\n]\nelev_reclass = service.getProcess(\"Coverage.reclass\").execute(dem_aligned, elev_rules, -9999)\n\n# Slope reclassification (example: 4 classes)\nslope_rules = [\n    (0.0, 5.0, 1.0),\n    (5.0, 15.0, 2.0),\n    (15.0, 25.0, 3.0),\n    (25.0, 90.0, 4.0)\n]\nslope_reclass = service.getProcess(\"Coverage.reclass\").execute(slope_aligned, slope_rules, -9999)\n\n# Aspect reclassification (example: 4 classes: flat, north/south, east/west)\naspect_rules = [\n    (-1.0, 0.0, 1.0),      # flat\n    (0.0, 45.0, 2.0),      # north\n    (45.0, 135.0, 3.0),    # east\n    (135.0, 225.0, 4.0),   # south\n    (225.0, 315.0, 5.0),   # west\n    (315.0, 360.0, 2.0)    # north\n]\naspect_reclass = service.getProcess(\"Coverage.reclass\").execute(aspect_aligned, aspect_rules, -9999)\n\n# ==================== 7. Geographical detector analysis ====================\n# Note:The geographical detector requires vector feature input, but only raster data is available here\n# The reclassified raster needs to be converted to vector data (no direct operator is currently available for this function)\n# Therefore, only the vegetation index distribution map is shown here; geographical detector analysis cannot currently be implemented because the raster-to-vector operator is missing\n# Note: the OGE platform currently lacks a \"raster-to-vector\" or \"raster sampling to points\" operator, so geographical detector analysis cannot be performed directly\n\n# ==================== 8. Result visualization ====================\n# Set NDVI visualization parameters\nvis_params_ndvi = {\n    \"min\": -1,\n    \"max\": 1,\n    \"palette\": [\"#d73027\", \"#f46d43\", \"#fdae61\", \"#fee08b\", \"#d9ef8b\", \"#a6d96a\", \"#66bd63\", \"#1a9850\"]\n}\n\n# Display the NDVI result\nndvi.styles(vis_params_ndvi).getMap(\"Vegetation Index (NDVI)\")\n\n# Set the map view (based on the Landsat image center)\noge.mapclient.centerMap(116.5, 28.8, 8)\n\n# ==================== 9. Output analysis results ====================\n# Export the NDVI result\nndvi.styles(vis_params_ndvi).export(\"vegetation_ndvi_analysis\")\n\n# Note: because raster-to-vector functionality is missing, quantitative analysis of terrain effects on vegetation (geographical detector) cannot currently be completed\n# It is recommended to add raster sampling or conversion functionality before conducting quantitative statistical analysis"
    code2="import oge\n\n# Initialize\noge.initialize()\nservice = oge.Service()\n\n# Read two image scenes\nimage_a = service.getCoverage(\n    coverageID=\"LC81230362016125LGN00\",\n    productID=\"LC08_L1T\"\n)\nimage_b = service.getCoverage(\n    coverageID=\"LC81230372016125LGN00\",\n    productID=\"LC08_L1T\"\n)\n\n# 1) Extract the main processing layer of image A (green band)\nband_a = service.getProcess(\"Coverage.selectBands\").execute(image_a, [\"B3\"])\n\n# 2) Extract the main processing layer of image B (green band)\nband_b = service.getProcess(\"Coverage.selectBands\").execute(image_b, [\"B3\"])\n\n# 3) Read the spatial reference information of the two image scenes separately\ncrs_a = service.getProcess(\"Coverage.projection\").execute(band_a)\ncrs_b = service.getProcess(\"Coverage.projection\").execute(band_b)\ncrs_a.log(\"band_a_crs\")\ncrs_b.log(\"band_b_crs\")\n\n# 4) Transform image A to the unified target coordinate system\nreprojected_a = service.getProcess(\"Coverage.reproject\").execute(\n    band_a,\n    3857,\n    30\n)\n\n# 5) Transform image B to the unified target coordinate system\nreprojected_b = service.getProcess(\"Coverage.reproject\").execute(\n    band_b,\n    3857,\n    30\n)\n\n# 6) Perform resolution harmonization for image A\nstandard_a = service.getProcess(\"Coverage.resampInterpByGrass\").execute(\n    reprojected_a,\n    \"30\",\n    \"bilinear\"\n)\n\n# 7) Perform resolution harmonization for image B\nstandard_b = service.getProcess(\"Coverage.resampInterpByGrass\").execute(\n    reprojected_b,\n    \"30\",\n    \"bilinear\"\n)\n\n# Visualization parameters\nvis_params = {\n    \"palette\": [\"#1f1f1f\", \"#5a5a5a\", \"#9a9a9a\", \"#d9d9d9\", \"#ffffff\"]\n}\n\n# 8) Organize a unified comparison structure for the two standardized scene results\nstandard_a.styles(vis_params).getMap(\"standard_a\")\nstandard_b.styles(vis_params).getMap(\"standard_b\")\n\n# Set the map center\noge.mapclient.centerMap(114.74, 33.88, 7)"
    code3 = "import oge\n\noge.initialize()\nservice = oge.Service()\n\nls8 = service.getCoverage(\n    coverageID=\"LC81220392015275LGN00\",\n    productID=\"LC08_L1T\"\n)\n\noriginal_display = service.getProcess(\"Coverage.selectBands\").execute(\n    ls8,\n    [\"B8\"]\n)\n\nchain_a_smooth = service.getProcess(\"Coverage.focalMean\").execute(\n    original_display,\n    \"square\",\n    1\n)\n\nkernel_a = service.getProcess(\"Kernel.prewitt\").execute(True, 0.5)\nchain_a_stage = service.getProcess(\"Coverage.convolve\").execute(\n    chain_a_smooth,\n    kernel_a\n)\n\nchain_a_final = service.getProcess(\"Coverage.focalMedian\").execute(\n    chain_a_stage,\n    \"circle\",\n    1\n)\n\nchain_b_smooth = service.getProcess(\"Coverage.focalMedian\").execute(\n    original_display,\n    \"circle\",\n    1\n)\n\nkernel_b = service.getProcess(\"Kernel.laplacian4\").execute()\nchain_b_stage = service.getProcess(\"Coverage.convolve\").execute(\n    chain_b_smooth,\n    kernel_b\n)\n\nchain_b_final = service.getProcess(\"Coverage.focalMean\").execute(\n    chain_b_stage,\n    \"square\",\n    1\n)\n\noriginal_vis = {\n    \"min\": 0,\n    \"max\": 30000,\n    \"palette\": [\"#1f1f1f\", \"#5a5a5a\", \"#9a9a9a\", \"#d9d9d9\", \"#ffffff\"]\n}\n\nenhance_vis = {\n    \"min\": -2000,\n    \"max\": 2000,\n    \"palette\": [\"#1f1f1f\", \"#5a5a5a\", \"#9a9a9a\", \"#d9d9d9\", \"#ffffff\"]\n}\n\noriginal_display.styles(original_vis).getMap(\"original_display\")\nchain_a_stage.styles(enhance_vis).getMap(\"chain_a_stage\")\nchain_a_final.styles(enhance_vis).getMap(\"chain_a_final\")\nchain_b_stage.styles(enhance_vis).getMap(\"chain_b_stage\")\nchain_b_final.styles(enhance_vis).getMap(\"chain_b_final\")\n\noge.mapclient.centerMap(114.30, 30.61, 10)"
    print(verify_code_to_dag(code3))
