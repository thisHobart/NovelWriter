# Agentic NovelWriter Implementation Plan

> ## ⚠️ 这是设计史料，不是现行架构
>
> 本文描述的多智能体编排层（`StoryGenerationOrchestrator`、`MultiAgentOrchestrator`、
> `QualityControlAgent`、`ConsistencyAgent`、checkpoint 断点续跑）**设计出来了，但从未
> 接到界面上**，已于 2026-09-07 删除。文中的导入路径和代码示例现在都跑不通。
>
> 实际在跑的是：
>
> | 做什么 | 在哪 |
> |---|---|
> | 四个阶段（设定/结构/场景/章节）依次执行 | `core/generation/stage_pipeline.py` |
> | 章节生成、质量闸门、定向重修 | `core/generation/chapter_generation_loop.py` |
> | 四份独立评审的合议 | `agents/review/domain_review_agent.py` |
> | 章节写作 | `agents/writing/chapter_writing_agent.py` |
> | 状态判断 | 一律以磁盘上的产物为准，不读任何断点记录 |
>
> 界面为什么逐阶段跑而不是交给一个总编排，见 `core/gui/services/step_runner.py`
> 中 `full_workflow_work` 的说明。端到端的现行流程见 `docs/workflow.md`。

## Overview

This document outlines the implementation plan for enhancing NovelWriter with agentic capabilities. Following the principles from "Don't Build Chatbots — Build Agents With Jobs" by Sean Falconer, we will create purpose-built agents that work together to improve novel generation quality, consistency, and adaptability.

## Design Philosophy

### Core Principles
- **Closed-World Problems**: Each agent handles specific, well-defined tasks with clear success criteria
- **Purpose-Built Tools**: Agents use specialized functions rather than generic APIs
- **Modular Architecture**: Clean separation between existing app and new agentic layer
- **Feedback Loops**: Iterative improvement through validation and revision cycles
- **Testable Outcomes**: Clear metrics for agent performance and story quality

### Separation of Concerns
- **Existing App**: Maintains current GUI-based workflow and core generation functions
- **Agentic Layer**: New orchestration system that can use existing functions as tools
- **Clean Interface**: Well-defined API between the two layers

## Architecture Overview

```
NovelWriter App (Existing)
├── GUI Components (Parameters, Lore, Structure, etc.)
├── Core Generation Functions
└── File I/O and State Management

Agentic Layer (New)
├── agents/
│   ├── quality_control_agent.py
│   ├── consistency_agent.py
│   ├── adaptive_planning_agent.py
│   └── orchestrator_agent.py
├── tools/
│   ├── story_analysis_tools.py
│   ├── content_validation_tools.py
│   └── revision_tools.py
├── evaluators/
│   ├── quality_evaluators.py
│   ├── consistency_evaluators.py
│   └── story_metrics.py
└── orchestration/
    ├── workflow_manager.py
    └── feedback_loops.py
```

## Implementation Phases

### Phase 1: Foundation & Quality Control Agent
**Timeline**: Week 1-2
**Goal**: Establish agentic architecture with basic quality validation

#### Components to Build:
1. **Base Agent Framework** (`agents/base_agent.py`)
   - Abstract base class for all agents
   - Common interfaces for tool usage and logging
   - Error handling and retry mechanisms

2. **Quality Control Agent** (`agents/quality_control_agent.py`)
   - Evaluates chapter coherence and quality
   - Provides specific improvement suggestions
   - Integrates with existing chapter writing workflow

3. **Story Analysis Tools** (`tools/story_analysis_tools.py`)
   - `analyze_chapter_coherence(chapter, context)`
   - `check_pacing(chapter, target_pacing)`
   - `evaluate_prose_quality(text, genre_standards)`

4. **Quality Evaluators** (`evaluators/quality_evaluators.py`)
   - Scoring functions for different quality aspects
   - Threshold definitions for acceptable quality
   - Reporting mechanisms for quality metrics

#### Integration Points:
- Hook into existing `ChapterWriting.generate_chapter()` method
- Add quality validation step before chapter finalization
- Provide feedback UI for quality suggestions

### Phase 2: Consistency & World-Building Agents (4-6 weeks)
**Status**: ✅ COMPLETED
**Goal**: Maintain story consistency and world-building coherence

#### Components to Build:
1. **Consistency Agent** (`agents/consistency_agent.py`)
   - Maintains character bible and world state
   - Validates new content against established facts
   - Tracks character development arcs

2. **World-Building Agent** (`agents/world_building_agent.py`)
   - Ensures consistent world rules and physics
   - Manages location descriptions and continuity
   - Handles technology/magic system consistency

3. **Content Validation Tools** (`tools/content_validation_tools.py`)
   - `validate_character_consistency(character, new_content, history)`
   - `check_world_rules(content, established_rules)`
   - `track_plot_threads(chapter, ongoing_threads)`

4. **Consistency Evaluators** (`evaluators/consistency_evaluators.py`)
   - Character trait consistency scoring
   - World-building violation detection
   - Plot thread tracking and resolution

#### Integration Points:
- Hook into lore generation and story structure phases
- Maintain persistent state files for character/world tracking
- Integrate with existing file I/O systems

### Phase 3: Adaptive Planning & Orchestration (6-8 weeks)
**Status**: 🚧 In Progress - Orchestration Complete
**Goal**: Dynamic story adaptation and intelligent orchestration

#### Components to Build:
1. **Adaptive Planning Agent** (`agents/adaptive_planning_agent.py`)
   - Analyzes story progress and pacing
   - Adjusts future scenes based on current state
   - Balances character development with plot advancement

2. **Orchestrator Agent** (`agents/orchestrator_agent.py`)
   - Coordinates all other agents
   - Makes high-level decisions about story direction
   - Manages feedback loops and revision cycles

3. **Workflow Manager** (`orchestration/workflow_manager.py`)
   - Defines agent interaction patterns
   - Manages execution order and dependencies
   - Handles error recovery and fallback strategies

4. **Feedback Loop System** (`orchestration/feedback_loops.py`)
   - Implements iterative improvement cycles
   - Manages revision thresholds and criteria
   - Coordinates multi-agent validation

#### Integration Points:
- Replace or enhance existing `generate_story()` method
- Add new "Agentic Mode" to the GUI
- Provide progress tracking and intervention points

## Technical Specifications

### Agent Communication Protocol
```python
class AgentMessage:
    def __init__(self, sender: str, recipient: str, message_type: str, content: dict):
        self.sender = sender
        self.recipient = recipient
        self.message_type = message_type  # 'request', 'response', 'notification'
        self.content = content
        self.timestamp = datetime.now()
```

### Tool Interface Standard
```python
class AgentTool:
    def __init__(self, name: str, description: str, parameters: dict):
        self.name = name
        self.description = description
        self.parameters = parameters  # JSON schema for parameters
    
    def execute(self, **kwargs) -> dict:
        """Execute the tool with given parameters"""
        pass
    
    def validate_parameters(self, **kwargs) -> bool:
        """Validate parameters against schema"""
        pass
```

### Quality Metrics Framework
```python
class QualityMetrics:
    coherence_score: float  # 0.0 - 1.0
    consistency_score: float  # 0.0 - 1.0
    pacing_score: float  # 0.0 - 1.0
    prose_quality: float  # 0.0 - 1.0
    genre_adherence: float  # 0.0 - 1.0
    
    def overall_score(self) -> float:
        """Weighted average of all metrics"""
        pass
```

## Integration Strategy

### Existing App Integration
1. **Minimal Changes**: Existing GUI and core functions remain unchanged
2. **Optional Mode**: Agentic features are opt-in, not mandatory
3. **Gradual Migration**: Users can choose which agents to enable
4. **Backward Compatibility**: All existing workflows continue to work

### Configuration Management
- New config section for agent settings
- Per-agent enable/disable flags
- Quality thresholds and preferences
- Model selection for different agents

### File Structure Integration
```
current_work/
├── [existing files]
├── agents/
│   ├── agent_logs/
│   ├── quality_reports/
│   ├── consistency_tracking/
│   └── revision_history/
└── agentic_config.json
```

## Testing Strategy

### Unit Testing
- Individual agent functionality
- Tool execution and validation
- Quality metric calculations
- Error handling and edge cases

### Integration Testing
- Agent communication protocols
- Workflow orchestration
- File I/O operations
- GUI integration points

### Quality Assurance
- Generated content evaluation
- Performance benchmarking
- User experience testing
- Regression testing for existing features

## Success Metrics

### Quantitative Metrics
- Quality score improvements (target: 15-20% increase)
- Consistency violation reduction (target: 80% fewer issues)
- User revision time reduction (target: 30% less manual editing)
- Story completion rate (target: maintain or improve current rates)

### Qualitative Metrics
- User satisfaction with generated content
- Perceived story coherence and quality
- Ease of use and learning curve
- Feature adoption rates

## Architectural Decisions

### Resolved Questions
1. **Model Selection**: ✅ Different models for different tasks, defaulting to same model for testing
2. **User Intervention**: ✅ Provide user options but also allow full agent autonomy
3. **Performance vs. Quality**: ✅ Speed not a concern initially, focus on quality
4. **Existing Workflow**: ✅ Integrate into existing GUI with new "Agentic Mode" tab
5. **Data Persistence**: ⏸️ Deferred for later phases
6. **Error Handling**: ⏸️ Will address in implementation

### Key Architectural Choices

#### GUI Integration Strategy
- **Approach**: Integrate into existing GUI rather than separate interface
- **Implementation**: New "Agentic Generation" tab + toggle switches in existing tabs
- **Benefits**: Familiar interface, single codebase, gradual adoption

#### Agent Decision-Making Model
- **Approach**: True agentic decision-making with tool selection
- **Implementation**: Agents receive available tools and decide which to use
- **Example**:
  ```python
  class QualityControlAgent:
      def __init__(self):
          self.available_tools = [
              "evaluate_coherence",
              "check_character_consistency", 
              "analyze_pacing",
              "suggest_improvements",
              "revise_content"
          ]
      
      def process_chapter(self, chapter, context):
          # LLM decides which tools to use and in what order
          plan = self.llm.plan_actions(chapter, context, self.available_tools)
          return self.execute_plan(plan)
  ```
- **Benefits**: Flexible, adaptive, handles edge cases, truly agentic

#### Directory Structure
```
NovelWriter/
├── [existing files]
├── agents/           # New agentic layer
│   ├── __init__.py
│   ├── base/
│   │   ├── agent.py
│   │   └── tool.py
│   ├── quality/
│   │   ├── quality_agent.py
│   │   └── quality_tools.py
│   ├── consistency/
│   │   ├── consistency_agent.py
│   │   └── consistency_tools.py
│   └── orchestration/
│       ├── orchestrator.py
│       └── workflow.py
```

## Next Steps

1. Review and refine this implementation plan
2. Set up the basic directory structure for the agentic layer
3. Begin Phase 1 implementation with the base agent framework
4. Create initial integration points with existing codebase
5. Develop testing framework and initial quality metrics

---

*This document will be updated as implementation progresses and requirements evolve.*
