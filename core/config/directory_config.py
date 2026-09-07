"""
Directory Configuration for NovelWriter

This module provides centralized directory path management to support
the new structured directory hierarchy while maintaining backward compatibility.
"""

import os
from typing import Dict, Optional
from dataclasses import dataclass


@dataclass
class DirectoryPaths:
    """Centralized directory path configuration."""
    
    # Root directories
    story_root: str = "story"
    quality_root: str = "quality" 
    system_root: str = "system"
    parameters_file: str = "system/parameters.txt"
    archive_root: str = "archive"
    
    # Story subdirectories
    lore_dir: str = "story/lore"
    structure_dir: str = "story/structure"
    planning_dir: str = "story/planning"
    content_dir: str = "story/content"
    
    # Planning subdirectories
    chapter_outlines_dir: str = "story/planning/chapter_outlines"
    scene_plans_dir: str = "story/planning/detailed_scene_plans"
    
    # Content subdirectories
    chapters_dir: str = "story/content/chapters"
    # 人物背景故事没有单独的子目录：lore_pipeline 直接把 background_<角色>_<名字>.md
    # 平铺在 lore_dir 下，structure_pipeline 也按平铺路径去读。这里曾经声明过一个
    # story/lore/backgrounds，结果是每个项目都凭空多出一个永远为空的目录。
    structure_outlines_dir: str = "story/structure/structure_outlines"
    
    # Quality subdirectories
    reviews_dir: str = "quality/reviews"
    scene_reviews_dir: str = "quality/reviews/scene_reviews"
    chapter_reviews_dir: str = "quality/reviews/chapter_reviews"
    batch_reviews_dir: str = "quality/reviews/batch_reviews"
    metrics_dir: str = "quality/metrics"
    reports_dir: str = "quality/reports"
    
    # System subdirectories
    logs_dir: str = "system/logs"
    prompts_dir: str = "system/prompts"
    metadata_dir: str = "system/metadata"
    
    # Archive subdirectories
    previous_versions_dir: str = "archive/previous_versions"
    failed_generations_dir: str = "archive/failed_generations"


class DirectoryManager:
    """Manages directory paths and provides backward compatibility."""
    
    def __init__(self, output_dir: str, use_new_structure: bool = False):
        """
        Initialize directory manager.
        
        Args:
            output_dir: Base output directory (e.g., "current_work")
            use_new_structure: Whether to use new structured directories
        """
        self.output_dir = output_dir
        self.use_new_structure = use_new_structure
        self.paths = DirectoryPaths()
        
        # Legacy path mappings for backward compatibility
        self.legacy_paths = {
            "detailed_scene_plans": "detailed_scene_plans",
            "chapters": "chapters",
            "prompts": "prompts"
        }
    
    def get_path(self, path_type: str) -> str:
        """
        Get the appropriate path based on current configuration.
        
        Args:
            path_type: Type of path needed (e.g., 'scene_plans_dir', 'chapters_dir')
            
        Returns:
            Full path relative to output_dir
        """
        if self.use_new_structure:
            # Use new structured paths
            return getattr(self.paths, path_type, path_type)
        else:
            # Use legacy flat structure
            return self.legacy_paths.get(path_type, path_type)
    
    def get_full_path(self, path_type: str) -> str:
        """Get full absolute path."""
        relative_path = self.get_path(path_type)
        return os.path.normpath(os.path.join(self.output_dir, relative_path))
    
    def ensure_directories_exist(self) -> None:
        """Create all necessary directories."""
        if self.use_new_structure:
            # Create new structured directories
            dirs_to_create = [
                self.paths.story_root,
                self.paths.lore_dir,
                self.paths.structure_dir,
                self.paths.planning_dir,
                self.paths.content_dir,
                self.paths.chapter_outlines_dir,
                self.paths.scene_plans_dir,
                self.paths.chapters_dir,
                self.paths.structure_outlines_dir,
                self.paths.quality_root,
                self.paths.reviews_dir,
                self.paths.scene_reviews_dir,
                self.paths.chapter_reviews_dir,
                self.paths.batch_reviews_dir,
                self.paths.metrics_dir,
                self.paths.reports_dir,
                self.paths.system_root,
                self.paths.logs_dir,
                self.paths.prompts_dir,
                self.paths.metadata_dir,
                self.paths.archive_root,
                self.paths.previous_versions_dir,
                self.paths.failed_generations_dir
            ]
        else:
            # Create legacy directories
            dirs_to_create = [
                "detailed_scene_plans",
                "chapters",
                "prompts"
            ]
        
        for dir_path in dirs_to_create:
            full_path = os.path.join(self.output_dir, dir_path)
            os.makedirs(full_path, exist_ok=True)
    
    def get_scene_plans_dir(self) -> str:
        """Get scene plans directory path (backward compatibility method)."""
        if self.use_new_structure:
            return self.paths.scene_plans_dir
        else:
            return "detailed_scene_plans"

    def get_parameters_path(self) -> str:
        """Return the canonical story-parameters file path."""
        if self.use_new_structure:
            return os.path.normpath(os.path.join(self.output_dir, self.paths.parameters_file))
        return os.path.normpath(os.path.join(self.output_dir, "parameters.txt"))

    def get_chapter_outlines_dir(self) -> str:
        """Get the directory containing generated chapter outlines."""
        if self.use_new_structure:
            return self.paths.chapter_outlines_dir
        return "."

    def get_chapter_outlines_path(self) -> str:
        """Return the full path to the generated chapter-outline directory."""
        return os.path.normpath(os.path.join(self.output_dir, self.get_chapter_outlines_dir()))
    
    def get_chapters_dir(self) -> str:
        """Get chapters directory path (backward compatibility method)."""
        if self.use_new_structure:
            return self.paths.chapters_dir
        else:
            return "chapters"
    
    def get_prompts_dir(self) -> str:
        """Get prompts directory path (backward compatibility method)."""
        if self.use_new_structure:
            return self.paths.prompts_dir
        else:
            return "prompts"
    
    def get_quality_dir(self) -> str:
        """Get quality directory path (for review system)."""
        if self.use_new_structure:
            return self.paths.quality_root
        else:
            # For legacy structure, create quality directory in root
            return "quality"
    
    def glob_files(self, pattern: str) -> list:
        """
        Find files matching a glob pattern relative to the output directory.
        
        Args:
            pattern: Glob pattern to match (e.g., "story/lore/*.md", "chapters/*.md")
            
        Returns:
            List of file paths relative to output_dir that match the pattern
        """
        import glob
        
        # Create full glob pattern
        full_pattern = os.path.join(self.output_dir, pattern)
        
        # Get matching files
        matching_files = glob.glob(full_pattern)
        
        # Convert back to relative paths
        relative_files = []
        for file_path in matching_files:
            relative_path = os.path.relpath(file_path, self.output_dir)
            relative_files.append(relative_path)
        
        return relative_files


# Global configuration - can be set by user preferences
DEFAULT_USE_NEW_STRUCTURE = False


def get_directory_manager(output_dir: str, use_new_structure: Optional[bool] = None) -> DirectoryManager:
    """
    Get a directory manager instance.
    
    Args:
        output_dir: Base output directory
        use_new_structure: Whether to use new structure (defaults to global setting)
        
    Returns:
        DirectoryManager instance
    """
    if use_new_structure is None:
        use_new_structure = DEFAULT_USE_NEW_STRUCTURE
    
    return DirectoryManager(output_dir, use_new_structure)


def set_global_structure_preference(use_new_structure: bool) -> None:
    """Set global preference for directory structure."""
    global DEFAULT_USE_NEW_STRUCTURE
    DEFAULT_USE_NEW_STRUCTURE = use_new_structure
