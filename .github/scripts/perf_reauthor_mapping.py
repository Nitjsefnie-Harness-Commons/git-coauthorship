from git_coauthorship import reauthor

mapping = reauthor.build_mapping(reauthor.get_all_commits(), 0, None,
                                 reauthor.build_trailer("Claude Opus 4.6", "noreply@anthropic.com"))
print(f"mapping {len(mapping)}")
