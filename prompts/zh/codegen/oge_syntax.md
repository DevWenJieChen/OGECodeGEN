- ### 一、初始化（固定写法）

  1. 必须先导入并初始化 OGE：

  ```
  import oge
  oge.initialize()
  service = oge.Service()
  ```

  1. 后续所有数据获取与处理操作必须通过 `service` 对象完成。

  ------

  ### 二、获取数据对象（Coverage / CoverageCollection / Feature / FeatureCollection）

  #### 2.1 获取单个 Coverage（最常见）

  使用 `getCoverage` 获取一个 Coverage 对象：

  ```
  cov = service.getCoverage(
      coverageID="<coverage_id>",
      productID="<product_id>"
  )
  ```

  约束：

  - coverageID：字符串字面量，某景影像的标识（例如：LC08_L1GT_121039_20240325_20240403_02_T2）
  - productID：字符串字面量，产品名称（例如 `"LC08_C02_L1"`）
  - 返回类型：`Coverage`

  #### 2.2 获取 CoverageCollection（范围 + 时间）

  当需要按空间范围或时间范围读取数据时，必须使用 `getCoverageCollection`：

  ```
  cov_col = service.getCoverageCollection(
      productID="<product_id>",
      bbox=[minLon, minLat, maxLon, maxLat],
      datetime=["<start_date>", "<end_date>"]
  )
  ```

  参数说明（必须严格遵守）：

  - productID：字符串，产品名称（例如 `"LC08_C02_L1"`）
  - bbox：`List[float]`，空间范围 `[minLon, minLat, maxLon, maxLat]`
  - datetime：`List[str]`，时间范围，例如 `["2024-01-01 00:00:00", "2024-03-01 00:00:00"]`

  重要类型约束（硬规则）：

  - `getCoverageCollection` 的返回类型是 `CoverageCollection`
  - `CoverageCollection` 不能直接使用任何 `Coverage` 算子
  - bboxCrs 可以用默认，除非必要不用额外指定

  #### 2.3 获取单个 Feature（矢量要素）
  当任务需要读取或构造单个矢量要素时，使用 Feature。

  **方式 A：从平台数据源读取单个 Feature**

  ```
  feature = service.getFeature(featureId="<feature_id_or_asset_path>")
  ```

  约束：

  - featureId：字符串字面量（例如 `"myData/EasternChina_PopulationAging_Vector.geojson"`）
  - 返回类型：`Feature`

  > 说明：Feature 的数据读取能力仍在发展，未来可能支持更多数据源与参数形式；当前以 `getFeature(featureId=...)` 为主。

  **方式 B：从几何对象构建（常用于示例/临时数据）**

  先构建 Geometry，再用 `Feature.loadFromGeometry` 生成 Feature：

  ```
  geom = service.getProcess("Geometry.Polygon").execute(coords, "EPSG:4326")
  feature = service.getProcess("Feature.loadFromGeometry").execute(geom, "{a:10}")
  ```

  约束：

  - Geometry 构造由 `Geometry.*` 算子生成（如 `Geometry.LineString / Geometry.Polygon`）
  - CRS 通常显式传入（例如 `"EPSG:4326"`）
  - `Feature.loadFromGeometry` 的属性参数常以字符串形式传入（例如 `"{a:10}"`）

  #### 2.4 获取 FeatureCollection（要素集合）

  当前常见方式：由 Feature 列表构建 FeatureCollection：

  FeatureCollection 表示一组矢量要素，常用于点集、线集、面集、上传 GeoJSON 数据、设施数据、道路数据、行政区数据等。

    常见方式 A：从上传矢量文件读取 FeatureCollection

    fc = service.getProcess("FeatureCollection.loadFeatureCollectionFromUpload").execute(
        "myData/example.geojson",
        "EPSG:4326"
    )

    适用场景：
    - data_info 或用户输入中给出 myData/*.geojson 等上传文件；
    - 任务涉及道路、设施点、行政区、多年份矢量数据等完整要素集合；
    - 后续需要使用 FeatureCollection.filterMetadata、FeatureCollection.size、FeatureCollection.intersection、FeatureCollection.difference 等集合算子。

    常见方式 B：从平台数据源读取 FeatureCollection

    fc = service.getFeatureCollection(featureId="<feature_collection_id_or_asset_path>")

    适用场景：
    - 平台提供的是 FeatureCollection 数据资产；
    - 输入本身是一个矢量数据集，而不是单个 Feature。

    常见方式 C：由若干 Feature 临时构建 FeatureCollection

    fc = service.getProcess("FeatureCollection.loadFromFeatureList").execute([feature1, feature2])

    适用场景：
    - 代码中临时构造了多个 Feature；
    - 需要把多个单要素组合成集合进行后续分析。
    - 不要把 loadFromFeatureList 当作读取上传 GeoJSON 数据集的默认方式。

  约束：

  - 输入：`List[Feature]`
  - 输出：`FeatureCollection`

  > 说明：FeatureCollection 的读取/构建方式也在发展，未来可能支持直接范围读取、数据源过滤等；当前以 “Feature 列表构建” 为主。

  ------

  ### 三、CoverageCollection → Coverage（必须的中间步骤）

  如果要使用 `Coverage` 的算子对 `CoverageCollection` 进行处理，必须先将其转换为 `Coverage`。

  唯一允许的方式是使用 `CoverageCollection.mosaic` 将所有影像镶嵌成一幅 Coverage 影像：

  ```
  cov = service.getProcess("CoverageCollection.mosaic").execute(cov_col)
  ```

  约束（硬性）：

  - 输入：`CoverageCollection`
  - 输出：`Coverage`
  - 只有在完成 mosaic 之后，才能继续使用 `Coverage` 的算子

  错误示例（禁止）：

  ```
  ndvi = service.getProcess("Coverage.normalizedDifference").execute(cov_col, ["B5", "B4"])
  ```

  正确示例：

  ```
  cov = service.getProcess("CoverageCollection.mosaic").execute(cov_col)
  ndvi = service.getProcess("Coverage.normalizedDifference").execute(cov, ["B5", "B4"])
  ```

  #### （补充）FeatureCollection 与 Feature 的关系说明（概括性规则）

  - `Feature` 与 `FeatureCollection` 的关系**类似** `Coverage` 与 `CoverageCollection`：前者是单对象，后者是集合。
  - 但目前 **FeatureCollection 不存在统一的“必须先转换为 Feature”** 的硬规则：
    - 很多算子是 `FeatureCollection.*`（如 filter/aggregate/geoHash），直接作用于集合；
    - 也有一些算子是 `Feature.*`（如 intersects），需要输入单个 Feature。
  - 若确实需要从集合得到单个要素或统计结果，应使用 `FeatureCollection.aggregate* / aggregateFirst` 等集合算子产生所需输出（可能是值、也可能是进一步可视化/栅格化的对象），而不是假设存在固定的“mosaic 类”转换。

  ------

  ### 四、获取 Process 实例

  使用 `getProcess` 获取处理算子：

  ```
  proc = service.getProcess("<process_id>")
  ```

  约束：

  - process_id：字符串字面量，例如：
    - Coverage 相关：`"Coverage.selectBands"`, `"Coverage.focalMean"`, `"Coverage.normalizedDifference"`, `"CoverageCollection.mosaic"`
    - Feature/Geometry 相关：`"Geometry.LineString"`, `"Geometry.Polygon"`, `"Feature.loadFromGeometry"`, `"Feature.intersects"`, `"FeatureCollection.filterMetadata"`, `"FeatureCollection.geoHash"`, `"FeatureCollection.aggregateCount"`

  ------

  ### 五、执行处理（Process.execute）

  #### 5.1 基本模式（推荐）

  ```
  out = service.getProcess("<process_id>").execute(arg1, arg2, ...)
  ```

  #### 5.2 参数规则（非常重要）

  1. 第一个参数通常是**主要数据对象**（Coverage / Feature / CoverageCollection / FeatureCollection）
  2. 后续参数必须按算子定义的顺序给出
  3. 使用位置参数，不使用关键字参数

  示例（Coverage）：

  ```
  b3 = service.getProcess("Coverage.selectBands").execute(ls8, ["B3"])
  mean = service.getProcess("Coverage.focalMean").execute(b3, "square", 1)
  ```

  示例（Feature/FeatureCollection）：

  ```
  fc2 = service.getProcess("FeatureCollection.filterMetadata").execute(fc, "a", "greater_than", "11")
  flag = service.getProcess("Feature.intersects").execute(feature1, feature2, "EPSG:4326")
  ```

  错误示例（禁止）：

  ```
  service.getProcess("Coverage.focalMean").execute(cov=b3, kernel="square", radius=1)
  ```

  ------

  ### 六、可视化与导出

  #### 6.1 可视化参数（Coverage 栅格）

  ```
  vis_params = {
      "min": <number>,
      "max": <number>,
      "palette": ["color1", "color2", ...]
  }
  ```

  palette 颜色顺序必须严格按数值从小到大映射；并且颜色选择要符合地物常识（例如水体蓝、植被绿等），避免突兀。

  #### 6.2 地图显示

  - Coverage（栅格）：

  ```
  cov.styles(vis_params).getMap("<layer_name>")
  ```

  - Feature / FeatureCollection（矢量，当前常见为颜色列表样式）：

  ```
  feature_or_fc.styles(["#FF0000"]).getMap("<layer_name>")
  ```

  > 说明：Feature 的样式能力仍在发展，当前示例以颜色列表为主；未来可能支持更丰富的符号化参数，但调用形式仍遵循 `.styles(...).getMap(...)`。

  #### 6.3 导出（不显示）

  ```
  cov.styles(vis_params).export("<layer_name>")
  ```

  （若 Feature/FeatureCollection 支持导出，遵循同样的 `.styles(...).export(...)` 形式。）

  ------

  ### 七、地图视角设置（非常重要，强烈建议）

  在线计算 / 地图展示模式下，必须显式设置地图视角：

  ```
  oge.mapclient.centerMap(lon, lat, zoom)
  ```

  关键说明：

  - 数据是否存在，取决于当前地图窗口对应的经纬范围
  - zoom 过大或中心点不在数据覆盖区域，可能无法读取到任何数据
  - 建议初始 zoom 不要过高（例如 6–8）
  - 不确定范围时降低 zoom，扩大覆盖范围

  ------

  ### 八、大模型生成代码的硬性约束（请严格遵守）

  1. 必须先写：

  ```
  import oge
  oge.initialize()
  service = oge.Service()
  ```

  2. 获取数据对象：

  - Coverage：

  ```
  cov = service.getCoverage(coverageID="...", productID="...")
  ```

  或范围读取：

  ```
  cov_col = service.getCoverageCollection(productID="...", bbox=[...], datetime=[...])
  ```

  - Feature（矢量）：

  ```
  feature = service.getFeature(featureId="...")
  ```

  或从几何构建（临时/示例）：

  ```
  geom = service.getProcess("Geometry.*").execute(..., "EPSG:4326")
  feature = service.getProcess("Feature.loadFromGeometry").execute(geom, "{...}")
  ```

  - FeatureCollection（集合）：

  ```
  # 若输入是上传的 GeoJSON / Shapefile 转换文件 / 多要素矢量数据集，优先读取为 FeatureCollection：
  fc = service.getProcess("FeatureCollection.loadFeatureCollectionFromUpload").execute(
    "myData/example.geojson",
    "EPSG:4326"
  )
  
  # 若平台提供 FeatureCollection 数据资产，也可以使用：
  
  fc = service.getFeatureCollection(featureId="...")
  
  # 若代码中临时构造了多个 Feature，再使用：
  
  fc = service.getProcess("FeatureCollection.loadFromFeatureList").execute([feature1, feature2, ...])
  ```
  
  3. 获取 Process 使用：

  ```
  proc = service.getProcess("...")
  ```
  
  4. 执行处理使用（位置参数）：
  
  ```
  out = proc.execute(arg1, arg2, ...)
  # 或
  out = service.getProcess("...").execute(arg1, arg2, ...)
  ```

  5. 类型硬规则：
  
  - 必须明确区分 `Coverage` 与 `CoverageCollection`；`CoverageCollection` 必须先 `mosaic` 才能用 `Coverage` 算子
  - 必须明确区分 `Feature` 与 `FeatureCollection`：
    - `Feature.*` 算子输入单要素
    - `FeatureCollection.*` 算子输入要素集合
    - 不要假设存在统一的 FeatureCollection→Feature 强制转换流程，按算子类型选择输入对象

  6. 出图或导出时，必须使用：
  
  - Coverage：

  ```
  cov.styles(vis_params).getMap("name")
  # 或
  cov.styles(vis_params).export("name")
  ```
  
  - Feature / FeatureCollection（当前常见）：
  
  ```
  obj.styles(["#RRGGBB"]).getMap("name")
  ```
  
  7. 尽量不要使用 `try/except`，会触发安全保护机制，不允许执行（OGE 平台执行，不是普通 python 解释器环境）。
  
  约束总结：
  
  - 初始化代码必须完整，顺序固定
  - 必须区分 Coverage/CoverageCollection 与 Feature/FeatureCollection
  - CoverageCollection 必须先 mosaic 才能使用 Coverage 算子
  - execute 使用位置参数，不使用关键字参数
  - 涉及地图显示或在线计算时，必须设置合理的 centerMap
  
  