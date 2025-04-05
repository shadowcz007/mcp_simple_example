import os
import sys
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.sse import SseServerTransport
import mcp.types as types
from starlette.applications import Starlette
from starlette.routing import Mount, Route 
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from urllib.parse import unquote
import logging
from datetime import datetime

MEMORY_VERSION="1.2.0"

# 定义数据结构 
@dataclass
class Entity:
    name: str
    entityType: str
    observations: list

@dataclass
class Relation:
    from_: str  # 使用 from_ 避免与 Python 关键字冲突
    to: str
    relationType: str

@dataclass
class KnowledgeGraph:
    entities: list
    relations: list

class KnowledgeGraphManager:
    def __init__(self, memory_path: str):
        self.memory_path = Path(memory_path).expanduser()
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.change_listeners = []  # 添加变更监听器列表

    def add_change_listener(self, listener):
        """添加知识图谱变更监听器"""
        self.change_listeners.append(listener)
        
    def notify_changes(self):
        """通知所有监听器知识图谱已变更"""
        for listener in self.change_listeners:
            listener()

    async def load_graph(self) -> KnowledgeGraph:
        try:
            if not self.memory_path.exists():
                return KnowledgeGraph(entities=[], relations=[])
                
            data = await self._read_file()
            graph = KnowledgeGraph(entities=[], relations=[])
            
            for line in data.split("\n"):
                if not line.strip():
                    continue
                    
                item = json.loads(line)
                if item["type"] == "entity":
                    graph.entities.append(Entity(
                        name=item["name"],
                        entityType=item["entityType"],
                        observations=item["observations"]
                    ))
                elif item["type"] == "relation":
                    graph.relations.append(Relation(
                        from_=item["from"],
                        to=item["to"],
                        relationType=item["relationType"]
                    ))
            
            return graph
            
        except Exception as e:
            print(f"Error loading graph: {e}")
            return KnowledgeGraph(entities=[], relations=[])

    async def save_graph(self, graph: KnowledgeGraph):
        try:
            lines = []
            for entity in graph.entities:
                lines.append(json.dumps({
                    "type": "entity",
                    "name": entity.name,
                    "entityType": entity.entityType,
                    "observations": entity.observations
                }, ensure_ascii=False))
                
            for relation in graph.relations:
                lines.append(json.dumps({
                    "type": "relation", 
                    "from": relation.from_,
                    "to": relation.to,
                    "relationType": relation.relationType
                }, ensure_ascii=False))
                
            await self._write_file("\n".join(lines))
            
        except Exception as e:
            print(f"Error saving graph: {e}")
            raise
        
        self.notify_changes()  # 通知变更

    async def _read_file(self) -> str:
        with open(self.memory_path, "r", encoding="utf-8") as f:
            return f.read()

    async def _write_file(self, content: str):
        with open(self.memory_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    async def create_entities(self, entities: list) -> list:
        graph = await self.load_graph()
        new_entities = [e for e in entities 
                       if not any(ex.name == e.name for ex in graph.entities)]
        graph.entities.extend(new_entities)
        await self.save_graph(graph) 
        return new_entities

    async def create_relations(self, relations: list) -> list:
        graph = await self.load_graph()
        new_relations = [r for r in relations 
                        if not any(ex.from_ == r.from_ and 
                                 ex.to == r.to and 
                                 ex.relationType == r.relationType 
                                 for ex in graph.relations)]
        graph.relations.extend(new_relations)
        await self.save_graph(graph)
        return new_relations

    async def add_observations(self, observations: list) -> list:
        graph = await self.load_graph()
        results = []
        
        for obs in observations:
            entity = next((e for e in graph.entities if e.name == obs["entityName"]), None)
            if not entity:
                raise ValueError(f"Entity with name {obs['entityName']} not found")
                
            new_observations = [c for c in obs["contents"] 
                              if c not in entity.observations]
            entity.observations.extend(new_observations)
            
            results.append({
                "entityName": obs["entityName"],
                "addedObservations": new_observations
            })
            
        await self.save_graph(graph)
        return results

    async def delete_entities(self, entity_names: list) -> None:
        graph = await self.load_graph()
        graph.entities = [e for e in graph.entities 
                         if e.name not in entity_names]
        graph.relations = [r for r in graph.relations 
                         if r.from_ not in entity_names and 
                         r.to not in entity_names]
        await self.save_graph(graph) 

    async def delete_observations(self, deletions: list) -> None:
        graph = await self.load_graph()
        
        for deletion in deletions:
            entity = next((e for e in graph.entities 
                          if e.name == deletion["entityName"]), None)
            if entity:
                entity.observations = [o for o in entity.observations 
                                     if o not in deletion["observations"]]
                
        await self.save_graph(graph)

    async def delete_relations(self, relations: list) -> None:
        graph = await self.load_graph()
        graph.relations = [r for r in graph.relations 
                         if not any(dr.from_ == r.from_ and 
                                  dr.to == r.to and 
                                  dr.relationType == r.relationType 
                                  for dr in relations)]
        await self.save_graph(graph)

    async def read_graph(self) -> KnowledgeGraph:
        return await self.load_graph()

    async def search_nodes(self, query: str) -> KnowledgeGraph:
        graph = await self.load_graph()
        #print(f"Searching for nodes with query: {query}")
        
        # 过滤实体
        filtered_entities = [e for e in graph.entities 
                           if (query.lower() in e.name.lower() or
                               query.lower() in e.entityType.lower() or
                               any(query.lower() in o.lower() 
                                   for o in e.observations))]
        
        # 创建过滤后的实体名称集合
        filtered_entity_names = {e.name for e in filtered_entities}

        #print(f"filtered_entities: {filtered_entity_names}")
        
        # 过滤关系
        filtered_relations = [r for r in graph.relations 
                            if r.from_ in filtered_entity_names and 
                            r.to in filtered_entity_names]
        
        return KnowledgeGraph(
            entities=filtered_entities,
            relations=filtered_relations
        )

    async def open_nodes(self, names: list) -> KnowledgeGraph:
        graph = await self.load_graph()
        
        # 过滤实体
        filtered_entities = [e for e in graph.entities 
                           if e.name in names]
        
        # 创建过滤后的实体名称集合
        filtered_entity_names = {e.name for e in filtered_entities}
        
        # 过滤关系
        filtered_relations = [r for r in graph.relations 
                            if r.from_ in filtered_entity_names and 
                            r.to in filtered_entity_names]
        
        return KnowledgeGraph(
            entities=filtered_entities,
            relations=filtered_relations
        )
    
def init_server(memory_path, log_level=logging.CRITICAL):
    # 添加日志设置
    if getattr(sys, 'frozen', False):
        log_path = Path(sys.executable).parent / "logs"
    else:
        log_path = Path(__file__).parent / "logs"
    
    # 创建日志目录
    log_path.mkdir(exist_ok=True)
    
    # 设置日志文件名（使用当前日期）
    log_file = log_path / f"memory_server_{datetime.now().strftime('%Y%m%d')}.log"
    
    # 配置日志
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()  # 同时输出到控制台
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Starting Memory MCP Server with memory path: {Path(memory_path).resolve()}")
    
    graph_manager = KnowledgeGraphManager(str(memory_path))

    app = Server("memory-manager",
                 version=MEMORY_VERSION,
                 instructions="This is a memory manager server for short story generation")

    # 将custom_initialization_options定义为独立函数
    def custom_initialization_options(
        server,  # 改为server参数而不是self
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        def pkg_version(package: str) -> str:
            try:
                from importlib.metadata import version
                v = version(package)
                if v is not None:
                    return v
            except Exception:
                pass
            return "unknown"
        # print(f"notification_options: {notification_options.resources_changed}")
        return InitializationOptions(
            server_name=server.name,
            server_version=server.version if server.version else pkg_version("mcp"),
            capabilities=server.get_capabilities(
                notification_options or NotificationOptions(
                    resources_changed=True,
                    tools_changed=True
                ),
                experimental_capabilities or {},
            ),
            instructions=server.instructions,
        )
    
    # 修改自定义初始化选项方法，确保启用资源变更通知
    app.create_initialization_options = lambda self=app: custom_initialization_options(
        self,
        notification_options=NotificationOptions(
            resources_changed=True,
            tools_changed=True,
            prompts_changed=True
        ),
        experimental_capabilities={"mix": {}}
    )
    
    # 添加资源变更通知函数
    async def notify_resources_changed():
        """通知客户端资源列表已变更"""
        logger = logging.getLogger(__name__)
        logger.debug("发送资源变更通知")
        try:
            await app.request_context.session.send_notification(
                types.ResourcesChangedNotification()
            )
        except Exception as e:
            logger.error(f"发送资源变更通知失败: {e}")
    
    # 将通知函数添加为知识图谱变更监听器
    def on_graph_changed():
        """当知识图谱变更时触发异步通知"""
        import asyncio
        asyncio.create_task(notify_resources_changed())
    
    graph_manager.add_change_listener(on_graph_changed)

    # 添加 prompt 功能
    @app.list_prompts()
    async def handle_list_prompts() -> list[types.Prompt]:
        return [types.Prompt(
            name="greeting-prompt",
            description="一个简单的问候提示模板",
            arguments=[
                types.PromptArgument(
                    name="name",
                    description="用户的名字",
                    required=True
                )
            ]
        ),
            types.Prompt(
                name="memory_chat",
                description="与记忆助手进行对话，助手会记住用户信息并更新知识图谱",
                systemPrompt="""
Follow these steps for each interaction:

1. User Identification:
   - You should assume that you are interacting with default_user
   - If you have not identified default_user, proactively try to do so.

2. Memory Retrieval:
   - Always begin your chat by saying only "Remembering..." and retrieve all relevant information from your knowledge graph
   - Always refer to your knowledge graph as your "memory"

3. Memory
   - While conversing with the user, be attentive to any new information that falls into these categories:
     a) Basic Identity (age, gender, location, job title, education level, etc.)
     b) Behaviors (interests, habits, etc.)
     c) Preferences (communication style, preferred language, etc.)
     d) Goals (goals, targets, aspirations, etc.)
     e) Relationships (personal and professional relationships up to 3 degrees of separation)

4. Memory Update:
   - If any new information was gathered during the interaction, update your memory as follows:
     a) Create entities for recurring organizations, people, and significant events
     b) Connect them to the current entities using relations
     b) Store facts about them as observations
"""
            ),
            types.Prompt(
                name="knowledge_extractor",
                description="从用户输入中提取关键知识点并创建实体和关系",
                systemPrompt="""
你是专业知识图谱构建专家，负责将非结构化文本转换为结构化知识。

【提取步骤】
1. 深入分析：仔细阅读用户输入，识别核心信息点
2. 实体提取：识别所有重要概念、人物、地点、组织、事件、产品等
3. 属性收集：为每个实体提取关键特征、描述和事实
4. 关系映射：确定实体间的逻辑连接和交互方式
5. 知识存储：使用工具函数将提取的知识保存到图谱中

【质量标准】
• 实体命名：精确、简洁、无歧义
• 类型分配：选择最贴合实体本质的类型（人物/地点/概念/组织/事件/产品等）
• 观察质量：客观、具体、信息丰富、避免重复
• 关系准确性：清晰表达实体间真实联系，使用恰当的关系类型

【工具使用指南】
• create_entities({
    "entities": [
        {"name": "实体名称", "entityType": "实体类型", "observations": ["观察1", "观察2"]}
    ]
})
• create_relations({
    "relations": [
        {"from_": "源实体名", "to": "目标实体名", "relationType": "关系类型"}
    ]
})

【示例分析】
输入：
"特斯拉是埃隆·马斯克创立的电动汽车公司，总部位于美国加州，其Model 3是全球最畅销的电动汽车之一。"

提取结果：
1. 实体：
   - {name: "特斯拉", entityType: "公司", observations: ["电动汽车公司", "总部位于美国加州", "生产Model 3车型"]}
   - {name: "埃隆·马斯克", entityType: "人物", observations: ["创立了特斯拉"]}
   - {name: "Model 3", entityType: "产品", observations: ["特斯拉生产", "全球最畅销的电动汽车之一"]}
   - {name: "美国加州", entityType: "地点", observations: ["特斯拉总部所在地"]}

2. 关系：
   - {from_: "埃隆·马斯克", to: "特斯拉", relationType: "创立"}
   - {from_: "特斯拉", to: "美国加州", relationType: "总部位于"}
   - {from_: "特斯拉", to: "Model 3", relationType: "生产"}

请直接分析用户输入并提取知识，无需解释你的分析过程。
"""
            )
        ]
        
    @app.get_prompt()
    async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        logger = logging.getLogger(__name__)
        logger.debug(f"Getting prompt: {name} with arguments: {arguments}")
        if name == "greeting-prompt":
            # 获取名字参数
            user_name = arguments.get("name", "用户") if arguments else "用户"
            # 返回提示模板结果
            return types.GetPromptResult(
                description=f"为{user_name}生成问候语",
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(
                            type="text", 
                            text=f"请为{user_name}生成一个友好的问候语。"
                        ),
                    )
                ],
            )


        if name == "knowledge_extractor":
            # 获取用户输入
            user_input = arguments.get("input", "") if arguments else ""

            name="knowledge_extractor"
            description="从用户输入中提取关键知识点并创建实体和关系"
            system_prompt="""
            你是专业知识图谱构建专家，负责将非结构化文本转换为结构化知识。

            【提取步骤】
            1. 深入分析：仔细阅读用户输入，识别核心信息点
            2. 实体提取：识别所有重要概念、人物、地点、组织、事件、产品等
            3. 属性收集：为每个实体提取关键特征、描述和事实
            4. 关系映射：确定实体间的逻辑连接和交互方式
            5. 知识存储：使用工具函数将提取的知识保存到图谱中

            【质量标准】
            • 实体命名：精确、简洁、无歧义
            • 类型分配：选择最贴合实体本质的类型（人物/地点/概念/组织/事件/产品等）
            • 观察质量：客观、具体、信息丰富、避免重复
            • 关系准确性：清晰表达实体间真实联系，使用恰当的关系类型

            【工具使用指南】
            • create_entities({
                "entities": [
                    {"name": "实体名称", "entityType": "实体类型", "observations": ["观察1", "观察2"]}
                ]
            })
            • create_relations({
                "relations": [
                    {"from_": "源实体名", "to": "目标实体名", "relationType": "关系类型"}
                ]
            })

            【示例分析】
            输入：
            "特斯拉是埃隆·马斯克创立的电动汽车公司，总部位于美国加州，其Model 3是全球最畅销的电动汽车之一。"

            提取结果：
            1. 实体：
            - {name: "特斯拉", entityType: "公司", observations: ["电动汽车公司", "总部位于美国加州", "生产Model 3车型"]}
            - {name: "埃隆·马斯克", entityType: "人物", observations: ["创立了特斯拉"]}
            - {name: "Model 3", entityType: "产品", observations: ["特斯拉生产", "全球最畅销的电动汽车之一"]}
            - {name: "美国加州", entityType: "地点", observations: ["特斯拉总部所在地"]}

            2. 关系：
            - {from_: "埃隆·马斯克", to: "特斯拉", relationType: "创立"}
            - {from_: "特斯拉", to: "美国加州", relationType: "总部位于"}
            - {from_: "特斯拉", to: "Model 3", relationType: "生产"}

            请直接分析用户输入{"""+user_input+"""}并提取知识，无需解释你的分析过程。
            """

            return types.GetPromptResult(
                description=description,
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(
                            type="text", 
                            text=system_prompt
                        ),
                    )
                ],
            )
        
        if name != "memory_chat":
            raise ValueError(f"Unknown prompt: {name}")
            
        # 获取当前请求上下文
        context = app.request_context
        # 从上下文中获取请求 ID
        request_id = context.request_id
        
        # 从知识图谱中获取用户信息
        graph = await graph_manager.read_graph()
        
        # 查找 default_user 实体
        default_user = next((e for e in graph.entities if e.name == "default_user"), None)
        
        # 构建用户信息上下文
        user_context = ""
        if default_user:
            user_context = f"用户信息:\n名称: {default_user.name}\n类型: {default_user.entityType}\n观察:\n"
            for obs in default_user.observations:
                user_context += f"- {obs}\n"
                
            # 添加与用户相关的关系
            related_relations = [r for r in graph.relations if r.from_ == "default_user" or r.to == "default_user"]
            if related_relations:
                user_context += "\n用户关系:\n"
                for relation in related_relations:
                    if relation.from_ == "default_user":
                        user_context += f"- {relation.from_} {relation.relationType} {relation.to}\n"
                    else:
                        user_context += f"- {relation.to} {relation.relationType} {relation.from_}\n"
        
        # 构建消息列表
        messages = []
        
        # 添加系统消息
        system_prompt = """
Follow these steps for each interaction:

1. User Identification:
   - You should assume that you are interacting with default_user
   - If you have not identified default_user, proactively try to do so.

2. Memory Retrieval:
   - Always begin your chat by saying only "Remembering..." and retrieve all relevant information from your knowledge graph
   - Always refer to your knowledge graph as your "memory"

3. Memory
   - While conversing with the user, be attentive to any new information that falls into these categories:
     a) Basic Identity (age, gender, location, job title, education level, etc.)
     b) Behaviors (interests, habits, etc.)
     c) Preferences (communication style, preferred language, etc.)
     d) Goals (goals, targets, aspirations, etc.)
     e) Relationships (personal and professional relationships up to 3 degrees of separation)

4. Memory Update:
   - If any new information was gathered during the interaction, update your memory as follows:
     a) Create entities for recurring organizations, people, and significant events
     b) Connect them to the current entities using relations
     b) Store facts about them as observations
"""
        
        if user_context:
            system_prompt += f"\n\n当前用户记忆:\n{user_context}"
        
        messages.append(
            types.SamplingMessage(
                role="system",
                content=types.TextContent(type="text", text=system_prompt)
            )
        )
        
        # 添加用户消息（如果有参数）
        if arguments and "message" in arguments:
            messages.append(
                types.SamplingMessage(
                    role="user",
                    content=types.TextContent(type="text", text=arguments["message"])
                )
            )
        
        return types.GetPromptResult(
            prompt=types.Prompt(
                name="memory_chat",
                description="与记忆助手进行对话，助手会记住用户信息并更新知识图谱",
                systemPrompt=system_prompt
            ),
            messages=messages
        )

    # 资源模板功能
    @app.list_resource_templates()
    async def handle_list_resource_templates() -> list[types.ResourceTemplate]:
        # A URI template (according to RFC 6570)
        return [
            types.ResourceTemplate(
                name="memory_template",
                uriTemplate="memory://short-story/{topic}",
                description="从知识图谱中读取相关信息并生成短故事",
                mimeType="text/plain"
            )
        ]
    
    # 资源
    @app.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        graph = await graph_manager.read_graph()
        entity_names = [entity.name for entity in graph.entities]
        logger.debug(f"handle_list_resources: {len(entity_names)} nodes found")

        return [types.Resource(
                name=name,
                uri=f"memory://short-story/{name}",
                description=f"主题{name}的短故事",
                mimeType="text/plain"
            )  for name in entity_names] +[
            types.Resource(
                name="topic",
                uri="memory://topic",
                description="从知识图谱中读取生成短故事的主题",
                mimeType="text/plain"
            )
        ]

    # 修改 handle_read_resource 添加日志
    @app.read_resource()
    async def handle_read_resource(uri) -> list[types.TextResourceContents]:
        logger = logging.getLogger(__name__)
        logger.debug(f"Reading resource with URI: {uri}")
        # 获取当前请求上下文
        context = app.request_context
        # 从上下文中获取请求 ID
        request_id = context.request_id
        try:
            # 检查URI格式
            if not str(uri).startswith("memory://"):
                error_msg = f"Invalid URI format: {uri}"
                logger.error(error_msg)
                raise ValueError(error_msg)
                
            # 从 URI 中提取并解码主题名称
            topic = str(uri).split('/')[-1]
            topic = unquote(topic)
            logger.debug(f"Extracted topic: {topic}")
            
            # 处理 "all" 请求 - 返回所有节点名称
            if str(uri) == "memory://topic":
                graph = await graph_manager.read_graph()
                entity_names = [entity.name for entity in graph.entities]
                logger.debug(f"Returning all node names: {len(entity_names)} nodes found")

                return [ReadResourceContents(
                                content=name,
                                mime_type="text/plain"
                            )  for name in entity_names]  
            
            # 搜索知识图谱中与主题相关的信息
            search_result = await graph_manager.search_nodes(topic)
            logger.debug(f"Search result: {len(search_result.entities)} entities, {len(search_result.relations)} relations")
            
            # 构建上下文信息
            context = []
            for entity in search_result.entities:
                logger.debug(f"Processing entity: {entity.name}")
                context.append(f"实体名称: {entity.name}")
                context.append(f"实体类型: {entity.entityType}")
                context.append("相关观察:")
                for obs in entity.observations:
                    context.append(f"- {obs}")
                context.append("")
            
            # 构建消息列表
            messages = []
            
            if context:
                content = "以下是与主题相关的已知信息：\n" + "\n".join(context)
                logger.debug("Context information built successfully")
            else:
                content = f"未找到与 [{topic}] 相关的信息"
                logger.warning(f"No information found for topic: {topic}")
            
            
            # 添加用户请求
            prompt = f"<userInput>{topic}</userInput>"+"\n------\n"+f"<context>{content}</context>"+"\n------\n"+"<story>200字以内，深刻体现背景信息，重点知识点标注出来，并附带知识点解释</story>"+f"\n\n要求：如果是有<context>，则结合<userInput>和<context>写一个短故事，故事要求见<story>。如果没有<context>信息，则根据<userInput>的要求进行。"
            messages.append(
                types.SamplingMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt)
                )
            )
            
            logger.debug("Sending sampling request")
            
            result = await app.request_context.session.create_message(
                max_tokens=1024,
                messages=messages,
                metadata={"topic": topic,"request_id":request_id}
            )
            logger.debug("Received response from sampling request")
            logger.debug("result.content.text: "+result.content.text)
            return result.content.text
            
        except Exception as e:
            error_msg = f"处理资源时出错: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return error_msg
        

    @app.list_tools()
    async def handle_list_tools():
        return [
            types.Tool(
                name="create_entities",
                description="Create multiple new entities in the knowledge graph",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entities": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "entityType": {"type": "string"},
                                    "observations": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    }
                                },
                                "required": ["name", "entityType", "observations"]
                            }
                        }
                    },
                    "required": ["entities"]
                }
            ),
            types.Tool(
                name="create_relations",
                description="Create multiple new relations between entities in the knowledge graph",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "relations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "from_": {"type": "string"},
                                    "to": {"type": "string"},
                                    "relationType": {"type": "string"}
                                },
                                "required": ["from_", "to", "relationType"]
                            }
                        }
                    },
                    "required": ["relations"]
                }
            ),
            types.Tool(
                name="add_observations",
                description="Add new observations to existing entities",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "observations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "entityName": {"type": "string"},
                                    "contents": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    }
                                },
                                "required": ["entityName", "contents"]
                            }
                        }
                    },
                    "required": ["observations"]
                }
            ),
            types.Tool(
                name="delete_entities",
                description="Delete multiple entities and their relations",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entityNames": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    "required": ["entityNames"]
                }
            ),
            types.Tool(
                name="delete_observations",
                description="Delete specific observations from entities",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deletions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "entityName": {"type": "string"},
                                    "observations": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    }
                                },
                                "required": ["entityName", "observations"]
                            }
                        }
                    },
                    "required": ["deletions"]
                }
            ),
            types.Tool(
                name="delete_relations",
                description="Delete multiple relations from the graph",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "relations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "from_": {"type": "string"},
                                    "to": {"type": "string"},
                                    "relationType": {"type": "string"}
                                },
                                "required": ["from_", "to", "relationType"]
                            }
                        }
                    },
                    "required": ["relations"]
                }
            ),
            types.Tool(
                name="read_graph",
                description="Read the entire knowledge graph",
                inputSchema={
                    "type": "object",
                    "properties": {}
                }
            ),
            types.Tool(
                name="search_nodes",
                description="Search for nodes in the graph",
                inputSchema={
                    "type": "object", 
                    "properties": {
                        "query": {"type": "string"}
                    },
                    "required": ["query"]
                }
            ),
            types.Tool(
                name="open_nodes",
                description="Open specific nodes by their names",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "names": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    "required": ["names"]
                }
            )
        ]

    @app.call_tool()
    async def handle_call_tool(
        name: str, 
        arguments: dict | None
    ) -> list:
        try:
            if name == "read_graph":
                result = await graph_manager.read_graph()
                return [types.TextContent(
                    type="text",
                    text=json.dumps({
                        "entities": [vars(e) for e in result.entities],
                        "relations": [vars(r) for r in result.relations]
                    }, indent=2, ensure_ascii=False)
                )]
            
            if not arguments:
                raise ValueError("Missing arguments")
                
            if name == "create_entities":
                entities = [Entity(**e) for e in arguments["entities"]]
                result = await graph_manager.create_entities(entities)
                return [types.TextContent(
                    type="text",
                    text=json.dumps([vars(e) for e in result], indent=2, ensure_ascii=False)
                )]
                
            elif name == "create_relations":
                relations = [Relation(**r) for r in arguments["relations"]]
                result = await graph_manager.create_relations(relations)
                return [types.TextContent(
                    type="text",
                    text=json.dumps([vars(r) for r in result], indent=2, ensure_ascii=False)
                )]
                
            elif name == "add_observations":
                result = await graph_manager.add_observations(arguments["observations"])
                return [types.TextContent(
                    type="text",
                    text=json.dumps(result, indent=2, ensure_ascii=False)
                )]
                
            elif name == "delete_entities":
                await graph_manager.delete_entities(arguments["entityNames"])
                return [types.TextContent(
                    type="text",
                    text="Entities deleted successfully"
                )]
                
            elif name == "delete_observations":
                await graph_manager.delete_observations(arguments["deletions"])
                return [types.TextContent(
                    type="text",
                    text="Observations deleted successfully"
                )]
                
            elif name == "delete_relations":
                relations = [Relation(**r) for r in arguments["relations"]]
                await graph_manager.delete_relations(relations)
                return [types.TextContent(
                    type="text",
                    text="Relations deleted successfully"
                )]
                
            elif name == "search_nodes":
                result = await graph_manager.search_nodes(arguments["query"])
                return [types.TextContent(
                    type="text",
                    text=json.dumps({
                        "entities": [vars(e) for e in result.entities],
                        "relations": [vars(r) for r in result.relations]
                    }, indent=2, ensure_ascii=False)
                )]
                
            elif name == "open_nodes":
                result = await graph_manager.open_nodes(arguments["names"])
                return [types.TextContent(
                    type="text",
                    text=json.dumps({
                        "entities": [vars(e) for e in result.entities],
                        "relations": [vars(r) for r in result.relations]
                    }, indent=2, ensure_ascii=False)
                )]
            
            else:
                raise ValueError(f"Unknown tool: {name}")
            
        except Exception as e:
            print(f"Error in tool {name}: {str(e)}")
            return [types.TextContent(type="text", text=f"Error: {str(e)}")]
    return app

async def main_sse(app, port: int = 8080):
    
    # 设置 SSE 服务器
    sse = SseServerTransport("/messages/")
    
    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(
                streams[0], streams[1], app.create_initialization_options()
            )
            
    # 添加 CORS 中间件配置        
    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],  # 允许所有来源，生产环境建议设置具体域名
            allow_credentials=True,
            allow_methods=["*"],  # 允许所有方法
            allow_headers=["*"],  # 允许所有请求头
        )
    ]
            
    starlette_app = Starlette(
        debug=True,
        routes=[
            Route("/", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        middleware=middleware  # 添加中间件配置
    )

    # 启动服务器
    import uvicorn
    import socket

    def get_local_ip():
        try:
            # 获取本机主机名
            hostname = socket.gethostname()
            # 获取本机IP地址
            ip = socket.gethostbyname(hostname)
            return ip
        except:
            return "127.0.0.1"

    local_ip = get_local_ip()
    print(f"\n🚀 服务器启动成功!")
    print(f"📡 本地访问地址: http://127.0.0.1:{port}")
    print(f"📡 局域网访问地址: http://{local_ip}:{port}")
    print("\n按 CTRL+C 停止服务器\n")

    config = uvicorn.Config(
        starlette_app, 
        host="0.0.0.0", 
        port=port,
        log_level="warning"  # 减少不必要的日志输出
    )
    server = uvicorn.Server(config)
    await server.serve()

def get_user_input(prompt: str, default: str) -> str:
    """获取用户输入，如果用户直接回车则使用默认值"""
    try:
        user_input = input(f"{prompt} (默认: {default}): ").strip()
        # 移除可能存在的 BOM
        if user_input.startswith('\ufeff'):
            user_input = user_input[1:]
        return user_input if user_input else default
    except Exception as e:
        print(f"输入处理错误: {e}")
        return default

def get_config_path() -> Path:
    """获取配置文件路径"""
    if getattr(sys, 'frozen', False):
        # 如果是打包后的exe运行
        return Path(sys.executable).parent / "config.json"
    else:
        # 如果是源代码运行
        return Path(__file__).parent / "config.json"

def load_config() -> dict:
    """加载配置文件"""
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_config(port: int, memory_path: str):
    """保存配置到文件"""
    config_path = get_config_path()
    config = {
        'port': port,
        'memory_path': str(memory_path)
    }
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存配置文件失败: {e}")

if __name__ == "__main__":
    import asyncio
    import argparse
    import sys

    # 创建命令行参数解析器
    parser = argparse.ArgumentParser(description='MCP Memory Server')
    parser.add_argument('--port', type=int, help='服务器端口号 (仅在 transport=sse 时需要)')
    parser.add_argument('--memory-path', type=str, help='内存文件路径')
    parser.add_argument('--transport', type=str, choices=['stdio', 'sse'], default='sse', help='传输类型 (stdio 或 sse)')
    
    args = parser.parse_args()

    if args.transport == 'stdio':
        from mcp.server.stdio import stdio_server
        async def run_stdio():

            if getattr(sys, 'frozen', False):
                memory_path = Path(sys.executable).parent / "memory.json"
            else:
                memory_path = Path(__file__).parent / "memory.json"
        
            app=init_server(str(memory_path))
            async with stdio_server() as (read_stream, write_stream):
                await app.run(
                    read_stream,
                    write_stream,
                    app.create_initialization_options()
                )
                
        asyncio.run(run_stdio())
        sys.exit(0)
    
    # 加载上次的配置
    last_config = load_config()
    port = None
    memory_path = None
    
    # 1. 首先检查是否有 stdin 输入
    try:
        # 检查stdin是否有数据可读
        if not sys.stdin.isatty():  # 如果stdin不是终端，说明可能有管道输入
            json_str = sys.stdin.read().strip()
            if json_str:  # 确保输入不为空
                if json_str.startswith('\ufeff'):
                    json_str = json_str[1:]
                stdin_config = json.loads(json_str)
                
                # 检查是否是帮助请求
                if (stdin_config.get("jsonrpc") == "2.0" and 
                    stdin_config.get("method") == "help" and 
                    "id" in stdin_config):
                    
                    help_response = {
                        "jsonrpc": "2.0",
                        "result": {
                            "type": "mcp",
                            "description": "此服务是提供memory相关的mcp服务",
                            "author": "shadow@Mixlab",
                            "version": MEMORY_VERSION,
                            "github": "https://github.com/shadowcz007/memory_mcp",
                            "transport": ["stdio", "sse"],
                            "methods": [
                                {
                                    "name": "help",
                                    "description": "显示此帮助信息。"
                                },
                                {
                                    "name": "start",
                                    "description": "启动服务器",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "transport": {
                                                "type": "string",
                                                "enum": ["stdio", "sse"],
                                                "description": "传输类型",
                                                "default": "sse"
                                            },
                                            "port": {
                                                "type": "integer",
                                                "description": "服务器端口号 (仅在 transport=sse 时需要设置)",
                                                "default": 8080
                                            },
                                            "memory_path": {
                                                "type": "string",
                                                "description": "内存文件路径",
                                                "default": "./memory.json"
                                            }
                                        }
                                    }
                                }
                            ]
                        },
                        "id": stdin_config["id"]
                    }
                    print(json.dumps(help_response, ensure_ascii=False, indent=2))
                    sys.exit(0)  # 退出程序，因为已经处理了请求

                # 新增处理 start 方法
                if (stdin_config.get("jsonrpc") == "2.0" and 
                    stdin_config.get("method") == "start" and 
                    "params" in stdin_config):
                    
                    params = stdin_config["params"]
                    transport = params.get("transport", "sse")
                    memory_path = params.get("memory_path", "./memory.json")
                    
                    # 只在 sse 模式下获取端口参数
                    if transport == "sse":
                        port = params.get("port")
                        if port is None:
                            port = 8080  # 默认端口
                    else:
                        port = None  # stdio 模式下不需要端口

                # port = stdin_config.get('port')
                # memory_path = stdin_config.get('memory_path')
                
                # 如果成功从stdin读取配置，直接使用这些值
                # if port is not None and memory_path is not None:
                #     print(f"从stdin读取配置: 端口={port}, 内存路径={memory_path}")
    except Exception as e:
        print(f"处理 stdin 输入时出错: {e}")
        # 继续执行，尝试其他配置方式
    
    # 获取 transport 参数
    transport = args.transport

    if transport != "stdio":
        # 如果没有 stdin 输入，检查命令行参数
        if port is None:
            port = args.port

        # 如果仍然没有配置，使用用户交互输入
        if port is None:
            default_port = str(last_config.get('port', 8080))
            if default_port == "None":
                default_port=8080
                
            port = int(get_user_input("请输入服务器端口号", default_port))

    if memory_path is None:
        memory_path = args.memory_path
        
    if memory_path is None:
        if getattr(sys, 'frozen', False):
            default_memory_path = Path(sys.executable).parent / "memory.json"
        else:
            default_memory_path = Path(__file__).parent / "memory.json"
            
        saved_memory_path = last_config.get('memory_path')
        default_path = saved_memory_path if saved_memory_path else str(default_memory_path)
        
        if transport == "sse":
            memory_path = get_user_input("请输入内存文件路径", default_path)
        else:
            memory_path = default_path

    # 处理内存文件路径
    memory_path = Path(memory_path)
    if not memory_path.is_absolute():
        if getattr(sys, 'frozen', False):
            memory_path = Path(sys.executable).parent / memory_path
        else:
            memory_path = Path(__file__).parent / memory_path
    
    print(f"Memory file will be stored at: {memory_path.resolve()}")
    
    
    # 根据 transport 类型处理端口参数
    if transport == "sse":
        if port is None:  # 如果之前没有从 stdin 或命令行获取到端口
            default_port = str(last_config.get('port', 8080))
            port = int(get_user_input("请输入服务器端口号", default_port))
            print(f"服务器将在端口 {port} 上运行")
    else:  # stdio 模式
        port = None
        print("使用 stdio 模式运行")
    
    # 保存配置并启动服务器
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    save_config(port, str(memory_path))

    if sys.platform == "darwin":  # Mac OS
        print("\033[1;32mStarting MCP Memory Server\033[0m")
        print("\033[1;34mby Mixlab - GitHub: https://github.com/shadowcz007/memory_mcp\033[0m")
        print("\033[1;36mTutorial: https://mp.weixin.qq.com/s/kiDlpgWqmo0eDYNd7Extmg\033[0m")
    else:  # Windows 和其他平台
        print("\033[1;32mStarting MCP Memory Server\033[0m")
        print("\033[1;34mby Mixlab \033]8;;https://github.com/shadowcz007/memory_mcp\033\\GitHub\033]8;;\033\\\033[0m")
        print("\033[1;36mTutorial: \033]8;;https://mp.weixin.qq.com/s/kiDlpgWqmo0eDYNd7Extmg\033\\点击查看教程\033]8;;\033\\\033[0m")
    print()
    print()

    # 根据 transport 类型启动不同的服务
    
    if transport == "sse":
        app=init_server(str(memory_path))
        asyncio.run(main_sse(app, port))
    
