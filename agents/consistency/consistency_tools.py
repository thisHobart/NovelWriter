"""
Consistency validation tools for NovelWriter agents.

These tools help maintain story consistency by tracking characters,
world-building elements, and plot threads across the narrative.
"""

from typing import Dict, List, Any, Optional, Set
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime

from agents.base.tool import BaseTool, ToolParameter, ToolResult, ToolAccessLevel
from core.generation.ai_helper import send_prompt


@dataclass
class CharacterState:
    """Tracks character information and development."""
    name: str
    traits: List[str]
    relationships: Dict[str, str]  # character_name -> relationship_type
    development_arc: List[str]     # key development points
    current_status: str            # alive, dead, missing, etc.
    last_seen_chapter: Optional[int] = None
    inconsistencies: List[str] = None
    
    def __post_init__(self):
        if self.inconsistencies is None:
            self.inconsistencies = []


@dataclass
class WorldElement:
    """Tracks world-building elements."""
    name: str
    type: str  # location, technology, magic_system, culture, etc.
    description: str
    rules: List[str]
    established_in_chapter: Optional[int] = None
    modifications: List[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.modifications is None:
            self.modifications = []


@dataclass
class PlotThread:
    """Tracks ongoing plot threads."""
    name: str
    description: str
    status: str  # active, resolved, abandoned
    introduced_chapter: int
    key_events: List[Dict[str, Any]]
    related_characters: List[str]
    resolution_chapter: Optional[int] = None


class ValidateCharacterConsistencyTool(BaseTool):
    """Tool to validate character consistency across chapters."""
    
    def __init__(self, model: Optional[str] = None):
        super().__init__(
            name="validate_character_consistency",
            description="Validate character traits, behavior, and development consistency",
            access_level=ToolAccessLevel.PUBLIC
        )
        self.model = model
    
    def _define_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="character_name",
                type="string",
                description="Name of the character to validate"
            ),
            ToolParameter(
                name="new_content",
                type="string",
                description="New content featuring this character"
            ),
            ToolParameter(
                name="character_history",
                type="object",
                description="Previous character information and development",
                required=False
            ),
            ToolParameter(
                name="context_chapters",
                type="array",
                description="Previous chapters for context",
                required=False
            )
        ]
    
    def _define_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "description": "Validate character consistency",
                "parameters": {
                    "character_name": "Sarah Chen",
                    "new_content": "Chapter content with character...",
                    "character_history": {
                        "traits": ["brave", "analytical"],
                        "relationships": {"Marcus": "friend"}
                    }
                },
                "expected_result": {
                    "consistency_score": 0.9,
                    "violations": [],
                    "character_updates": {}
                }
            }
        ]
    
    def _execute(self, **kwargs) -> ToolResult:
        character_name = kwargs["character_name"]
        new_content = kwargs["new_content"]
        character_history = kwargs.get("character_history", {})
        context_chapters = kwargs.get("context_chapters", [])
        
        try:
            # Analyze character consistency
            analysis = self._analyze_character_consistency(
                character_name, new_content, character_history, context_chapters
            )
            
            return ToolResult(
                success=True,
                data=analysis,
                metadata={
                    "character_name": character_name,
                    "model_used": self.model
                }
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error_message=f"Character consistency validation failed: {str(e)}"
            )
    
    def _analyze_character_consistency(self, name: str, content: str, 
                                     history: Dict, context: List[str]) -> Dict[str, Any]:
        """Analyze character consistency using LLM."""
        
        # Create analysis prompt
        prompt = self._create_consistency_prompt(name, content, history, context)
        
        # Get LLM analysis
        response = send_prompt(prompt, model=self.model)
        
        # Parse response
        analysis = self._parse_consistency_response(response)
        
        # Add character updates based on new content
        analysis["character_updates"] = self._extract_character_updates(name, content)
        
        return analysis
    
    def _create_consistency_prompt(self, name: str, content: str, 
                                 history: Dict, context: List[str]) -> str:
        """Create prompt for character consistency analysis."""
        
        context_text = "\n".join(context[-2:]) if context else "没有前文背景"
        
        return f"""
分析人物 {name} 在新内容中的一致性。

人物历史：
{json.dumps(history, indent=2) if history else "没有既往记录"}

前文背景：
{context_text}

新内容：
{content}

分析标准：
1. 性格特征一致性
2. 行为模式一致性
3. 人物关系一致性
4. 人物成长进程
5. 外貌描写一致性
6. 说话方式一致性

请使用 JSON 格式提供分析：
{{
    "consistency_score": 0.0-1.0,
    "violations": [
        {{
            "type": "trait/behavior/relationship/development",
            "description": "发现的具体不一致",
            "severity": "minor/moderate/major"
        }}
    ],
    "character_insights": [
        "观察到的新特征或行为",
        "人物成长情况"
    ],
    "recommendations": [
        "保持一致性的建议"
    ]
}}
"""
    
    def _parse_consistency_response(self, response: str) -> Dict[str, Any]:
        """Parse LLM consistency analysis response."""
        try:
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
        
        # Fallback parsing
        return {
            "consistency_score": 0.8,
            "violations": [],
            "character_insights": [],
            "recommendations": ["Manual review recommended"]
        }
    
    def _extract_character_updates(self, name: str, content: str) -> Dict[str, Any]:
        """Extract character information updates from new content."""
        # Simple extraction - could be enhanced with NLP
        updates = {}
        
        # Look for dialogue to update speech patterns
        dialogue_matches = re.findall(rf'"{name}[^"]*"([^"]+)"', content, re.IGNORECASE)
        if dialogue_matches:
            updates["recent_dialogue"] = dialogue_matches[:3]  # Keep recent examples
        
        # Look for character descriptions
        desc_patterns = [
            rf'{name}.*?(?:was|is|looked|appeared|seemed).*?(?:\.|,)',
            rf'(?:his|her|their).*?(?:eyes|hair|face|voice).*?(?:\.|,)'
        ]
        
        descriptions = []
        for pattern in desc_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            descriptions.extend(matches)
        
        if descriptions:
            updates["recent_descriptions"] = descriptions[:2]
        
        return updates


class TrackWorldBuildingTool(BaseTool):
    """Tool to track and validate world-building elements."""
    
    def __init__(self, model: Optional[str] = None):
        super().__init__(
            name="track_world_building",
            description="Track and validate world-building elements and rules",
            access_level=ToolAccessLevel.PUBLIC
        )
        self.model = model
    
    def _define_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="content",
                type="string",
                description="Content to analyze for world-building elements"
            ),
            ToolParameter(
                name="established_world",
                type="object",
                description="Previously established world-building elements",
                required=False
            ),
            ToolParameter(
                name="genre",
                type="string",
                description="Story genre for context",
                required=False,
                default="general"
            )
        ]
    
    def _execute(self, **kwargs) -> ToolResult:
        content = kwargs["content"]
        established_world = kwargs.get("established_world", {})
        genre = kwargs.get("genre", "general")
        
        try:
            # Extract world-building elements
            world_analysis = self._analyze_world_building(content, established_world, genre)
            
            return ToolResult(
                success=True,
                data=world_analysis,
                metadata={"genre": genre, "model_used": self.model}
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error_message=f"World-building analysis failed: {str(e)}"
            )
    
    def _analyze_world_building(self, content: str, established: Dict, genre: str) -> Dict[str, Any]:
        """Analyze world-building elements in content."""
        
        prompt = f"""
分析以下 {genre} 内容中的世界构建元素。

既有世界元素：
{json.dumps(established, indent=2) if established else "没有既有元素"}

新内容：
{content}

分析任务：
1. 识别新的世界构建元素（地点、技术、魔法、文化等）
2. 检查是否符合既有世界规则
3. 指出矛盾或规则冲突
4. 提出扩展世界构建的建议

请使用 JSON 格式提供分析：
{{
    "new_elements": [
        {{
            "name": "元素名称",
            "type": "location/technology/culture/rule/etc",
            "description": "已建立的内容",
            "rules": ["相关规则或属性"]
        }}
    ],
    "consistency_violations": [
        {{
            "element": "发生冲突的元素",
            "violation": "不一致说明",
            "severity": "minor/moderate/major"
        }}
    ],
    "world_expansion_opportunities": [
        "扩展世界构建的建议"
    ]
}}
"""
        
        response = send_prompt(prompt, model=self.model)
        return self._parse_world_response(response)
    
    def _parse_world_response(self, response: str) -> Dict[str, Any]:
        """Parse world-building analysis response."""
        try:
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
        
        return {
            "new_elements": [],
            "consistency_violations": [],
            "world_expansion_opportunities": []
        }


class TrackPlotThreadsTool(BaseTool):
    """Tool to track and manage plot threads."""
    
    def __init__(self, model: Optional[str] = None):
        super().__init__(
            name="track_plot_threads",
            description="Track ongoing plot threads and their resolution",
            access_level=ToolAccessLevel.PUBLIC
        )
        self.model = model
    
    def _define_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="content",
                type="string",
                description="Content to analyze for plot thread updates"
            ),
            ToolParameter(
                name="active_threads",
                type="array",
                description="Currently active plot threads",
                required=False
            ),
            ToolParameter(
                name="chapter_number",
                type="number",
                description="Current chapter number",
                required=False
            )
        ]
    
    def _execute(self, **kwargs) -> ToolResult:
        content = kwargs["content"]
        active_threads = kwargs.get("active_threads", [])
        chapter_number = kwargs.get("chapter_number", 1)
        
        try:
            # Analyze plot threads
            thread_analysis = self._analyze_plot_threads(content, active_threads, chapter_number)
            
            return ToolResult(
                success=True,
                data=thread_analysis,
                metadata={"chapter_number": chapter_number, "model_used": self.model}
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error_message=f"Plot thread analysis failed: {str(e)}"
            )
    
    def _analyze_plot_threads(self, content: str, active_threads: List, chapter_num: int) -> Dict[str, Any]:
        """Analyze plot thread progression."""
        
        prompt = f"""
分析第 {chapter_num} 章中的情节线进展。

活跃情节线：
{json.dumps(active_threads, indent=2) if active_threads else "没有活跃情节线"}

章节内容：
{content}

分析任务：
1. 识别哪些活跃情节线得到推进
2. 识别新引入的情节线
3. 指出已经解决的情节线
4. 检查被搁置或遗忘的情节线

请使用 JSON 格式提供分析：
{{
    "thread_updates": [
        {{
            "thread_name": "既有情节线名称",
            "advancement": "情节线如何推进",
            "status": "active/resolved/stalled"
        }}
    ],
    "new_threads": [
        {{
            "name": "新情节线名称",
            "description": "情节线内容",
            "characters_involved": ["涉及人物"]
        }}
    ],
    "resolved_threads": [
        {{
            "name": "已解决情节线名称",
            "resolution": "解决方式"
        }}
    ],
    "potential_issues": [
        "可能被遗忘或需要关注的情节线"
    ]
}}
"""
        
        response = send_prompt(prompt, model=self.model)
        return self._parse_thread_response(response)
    
    def _parse_thread_response(self, response: str) -> Dict[str, Any]:
        """Parse plot thread analysis response."""
        try:
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
        
        return {
            "thread_updates": [],
            "new_threads": [],
            "resolved_threads": [],
            "potential_issues": []
        }
